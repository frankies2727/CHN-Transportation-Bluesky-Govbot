#!/usr/bin/env python3
"""
Filter govbot's bills.jsonl for transportation-related bills across all 50
states + DC, dedupe against state/posted.json, summarize with Claude, and
post to Bluesky with rich link-card embeds.

Bill links go to openstates.org — a unified bill viewer maintained by the
Open States project. Each bill page also links back to the state's official
source. Far more reliable than hand-building 50 different state URL patterns.
"""

from __future__ import annotations

import io
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse, urljoin

import requests

ROOT = Path(__file__).resolve().parent.parent
JSONL_PATH = ROOT / "bills.jsonl"
STATE_FILE = ROOT / "state" / "posted.json"

POST_LIMIT = int(os.environ.get("POST_LIMIT", "3"))  # how many bluesky posts per run
DRY_RUN = os.environ.get("DRY_RUN") == "1"

BSKY_HANDLE = os.environ.get("BLUESKY_HANDLE", "")
BSKY_PASSWORD = os.environ.get("BLUESKY_APP_PASSWORD", "")
ANTHROPIC_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

BLUESKY_API = "https://bsky.social/xrpc"
ANTHROPIC_API = "https://api.anthropic.com/v1/messages"
ANTHROPIC_MODEL = "claude-haiku-4-5-20251001"

IMG_MAX_DOWNLOAD = 5 * 1024 * 1024
IMG_TARGET_SIZE  = 900 * 1024
IMG_FETCH_TIMEOUT = 10
USER_AGENT = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

US_STATES = {
    "AL","AK","AZ","AR","CA","CO","CT","DE","FL","GA","HI","ID","IL","IN","IA",
    "KS","KY","LA","ME","MD","MA","MI","MN","MS","MO","MT","NE","NV","NH","NJ",
    "NM","NY","NC","ND","OH","OK","OR","PA","RI","SC","SD","TN","TX","UT","VT",
    "VA","WA","WV","WI","WY","DC","PR","GU","VI","AS","MP",
}

STATE_FULL_NAME = {
    "AL":"Alabama","AK":"Alaska","AZ":"Arizona","AR":"Arkansas","CA":"California",
    "CO":"Colorado","CT":"Connecticut","DE":"Delaware","FL":"Florida","GA":"Georgia",
    "HI":"Hawaii","ID":"Idaho","IL":"Illinois","IN":"Indiana","IA":"Iowa",
    "KS":"Kansas","KY":"Kentucky","LA":"Louisiana","ME":"Maine","MD":"Maryland",
    "MA":"Massachusetts","MI":"Michigan","MN":"Minnesota","MS":"Mississippi","MO":"Missouri",
    "MT":"Montana","NE":"Nebraska","NV":"Nevada","NH":"New Hampshire","NJ":"New Jersey",
    "NM":"New Mexico","NY":"New York","NC":"North Carolina","ND":"North Dakota","OH":"Ohio",
    "OK":"Oklahoma","OR":"Oregon","PA":"Pennsylvania","RI":"Rhode Island","SC":"South Carolina",
    "SD":"South Dakota","TN":"Tennessee","TX":"Texas","UT":"Utah","VT":"Vermont",
    "VA":"Virginia","WA":"Washington","WV":"West Virginia","WI":"Wisconsin","WY":"Wyoming",
    "DC":"Washington D.C.","PR":"Puerto Rico",
}

TRANSPORTATION_KEYWORDS = [
    "transportation", "transit",
    "rail", "railroad", "railway", "amtrak", "metra",
    "subway", "streetcar", "light rail", "commuter rail",
    "ferry",
    "bicycle", "bicyclist", "bike lane", "cyclist",
    "pedestrian", "sidewalk", "crosswalk", "walkability",
    "airport", "aviation", "airline", "aircraft",
    "freight", "trucking", "commercial vehicle",
    "rideshare", "ride-share", "ride share",
    "ev charging", "electric vehicle", "autonomous vehicle",
    "scooter", "e-bike",
    "school bus", "bus driver", "bus drivers",
    "highway", "tollway", "roadway", "expressway", "interstate",
    "tollbooth", "toll road", "toll bridge",
    "traffic signal", "traffic safety", "road construction",
    "complete streets", "vision zero", "pedestrian safety",
    "transportation infrastructure",
    "motor vehicle", "motor fuel tax", "gas tax",
    "vehicle registration", "license plate", "driver's license",
    "speed limit", "seatbelt", "helmet law",
    "parking", "congestion", "traffic",
]

_KEYWORD_PATTERN = re.compile(
    r"\b(" + "|".join(re.escape(k) for k in TRANSPORTATION_KEYWORDS) + r")\b",
    re.IGNORECASE,
)

MAX_POST = 290
LINK_PREFIX = "🔗 "


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------

def load_bills(path: Path) -> list[dict]:
    if not path.exists():
        print(f"ERROR: {path} does not exist. Did `govbot logs` run?", file=sys.stderr)
        return []
    bills = []
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                bills.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    print(f"Loaded {len(bills)} records from {path.name}")
    return bills


# ---------------------------------------------------------------------------
# State detection
# ---------------------------------------------------------------------------

_STATE_TAG_PATTERN = re.compile(r"\bstate:([a-z]{2})\b", re.IGNORECASE)


def _walk_strings(obj):
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for v in obj.values():
            yield from _walk_strings(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _walk_strings(v)


def detect_state(record: dict) -> str:
    for s in _walk_strings(record):
        m = _STATE_TAG_PATTERN.search(s)
        if m:
            code = m.group(1).upper()
            if code in US_STATES:
                return code
    return ""


# ---------------------------------------------------------------------------
# Field extraction
# ---------------------------------------------------------------------------

def _looks_like_code_title(title: str) -> bool:
    t = title.strip()
    if not t:
        return True
    letters = [c for c in t if c.isalpha()]
    if not letters:
        return False
    upper_ratio = sum(1 for c in letters if c.isupper()) / len(letters)
    return len(t) < 35 and upper_ratio > 0.7


def extract_fields(record: dict) -> dict | None:
    bill = record.get("bill") or {}
    log = record.get("log") or {}

    identifier = bill.get("identifier") or record.get("id") or ""
    title = bill.get("title") or ""
    if not identifier or not title:
        return None

    state = detect_state(record)
    session = bill.get("legislative_session") or ""

    abstract = ""
    for a in (bill.get("abstracts") or []):
        text = a.get("abstract", "") if isinstance(a, dict) else (a if isinstance(a, str) else "")
        if text:
            abstract = text
            break

    subjects = bill.get("subject") or []
    subjects_text = " ".join(str(s) for s in subjects) if isinstance(subjects, list) else str(subjects or "")

    action = log.get("action") or {}
    action_desc = action.get("description") or ""
    action_date_raw = action.get("date") or ""
    action_date = action_date_raw[:10] if action_date_raw else ""

    dedup_key = f"{state}|{identifier}|{action_date}|{action_desc[:40]}"
    same_day_key = f"{state}|{identifier}|{action_date}"

    return {
        "state": state,
        "session": session,
        "identifier": identifier,
        "title": title,
        "abstract": abstract,
        "subjects": subjects_text,
        "action_desc": action_desc,
        "action_date": action_date,
        "dedup_key": dedup_key,
        "same_day_key": same_day_key,
    }


def is_transportation(b: dict) -> bool:
    haystack = " ".join([b["title"], b["abstract"], b["subjects"]]).lower()
    return bool(_KEYWORD_PATTERN.search(haystack))


def best_display_text(b: dict) -> str:
    if _looks_like_code_title(b["title"]) and b["abstract"]:
        return b["abstract"]
    return b["title"]


# ---------------------------------------------------------------------------
# Action + date formatting
# ---------------------------------------------------------------------------

def _format_date(yyyy_mm_dd: str) -> str:
    try:
        d = datetime.strptime(yyyy_mm_dd, "%Y-%m-%d")
    except ValueError:
        return ""
    abbrev = {1:"Jan.", 2:"Feb.", 3:"March", 4:"April", 5:"May", 6:"June",
              7:"July", 8:"Aug.", 9:"Sept.", 10:"Oct.", 11:"Nov.", 12:"Dec."}
    return f"{abbrev[d.month]} {d.day}, {d.year}"


def _smart_case(s: str) -> str:
    s = s.strip().rstrip(".")
    if not s:
        return s
    letters = [c for c in s if c.isalpha()]
    if letters and sum(1 for c in letters if c.isupper()) / len(letters) > 0.7:
        small = {"a","an","and","of","or","the","to","by","in","on","for","with","at"}
        words = s.lower().split()
        out = []
        for i, w in enumerate(words):
            out.append(w.capitalize() if (i == 0 or w not in small) else w)
        return " ".join(out)
    return s[0].upper() + s[1:] if s[0].isalpha() else s


def format_action_line(action_desc: str, date_yyyy_mm_dd: str) -> str:
    desc = _smart_case(action_desc)
    nice_date = _format_date(date_yyyy_mm_dd)
    if desc and nice_date:
        desc_with_period = desc if desc.endswith((".", "!", "?")) else desc + "."
        return f"{nice_date}: {desc_with_period}"
    return ""


# ---------------------------------------------------------------------------
# OG image fetching
# ---------------------------------------------------------------------------

_OG_IMAGE_PATTERNS = [
    re.compile(r'<meta\s+property=["\']og:image["\']\s+content=["\']([^"\']+)["\']', re.IGNORECASE),
    re.compile(r'<meta\s+content=["\']([^"\']+)["\']\s+property=["\']og:image["\']', re.IGNORECASE),
    re.compile(r'<meta\s+name=["\']twitter:image["\']\s+content=["\']([^"\']+)["\']', re.IGNORECASE),
    re.compile(r'<meta\s+content=["\']([^"\']+)["\']\s+name=["\']twitter:image["\']', re.IGNORECASE),
]


def _extract_og_image_url(html: str, base_url: str) -> str:
    head_only = html[:40000]
    for pat in _OG_IMAGE_PATTERNS:
        m = pat.search(head_only)
        if m:
            url = m.group(1).strip().replace("&amp;", "&")
            return urljoin(base_url, url)
    return ""


def _requests_get_lenient(url, **kwargs):
    try:
        return requests.get(url, **kwargs)
    except requests.exceptions.SSLError:
        print(f"  IMG: SSL verify failed, retrying without verification...")
        kwargs2 = dict(kwargs)
        kwargs2["verify"] = False
        try:
            import urllib3
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        except Exception:
            pass
        return requests.get(url, **kwargs2)


def fetch_og_image(page_url: str) -> tuple[bytes, str] | None:
    try:
        page_host = urlparse(page_url).netloc.lower()
        if not page_host:
            return None

        headers = {
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Accept-Encoding": "gzip, deflate, br",
        }

        r = _requests_get_lenient(page_url, headers=headers, timeout=IMG_FETCH_TIMEOUT, stream=True)
        r.raise_for_status()
        ctype = r.headers.get("content-type", "").lower()
        if "html" not in ctype:
            return None

        html_bytes = b""
        for chunk in r.iter_content(chunk_size=8192):
            html_bytes += chunk
            if len(html_bytes) > 500_000:
                break
        try:
            html = html_bytes.decode("utf-8", errors="replace")
        except Exception:
            return None

        img_url = _extract_og_image_url(html, page_url)
        if not img_url:
            return None

        img_host = urlparse(img_url).netloc.lower()
        if img_host and img_host != page_host:
            if img_host.lstrip("www.") != page_host.lstrip("www."):
                print(f"  IMG: ✗ og:image is off-site ({img_host}), skipping")
                return None

        ir = _requests_get_lenient(img_url, headers=headers, timeout=IMG_FETCH_TIMEOUT, stream=True)
        ir.raise_for_status()

        img_bytes = b""
        for chunk in ir.iter_content(chunk_size=16384):
            img_bytes += chunk
            if len(img_bytes) > IMG_MAX_DOWNLOAD:
                print(f"  IMG: ✗ og:image too large (>{IMG_MAX_DOWNLOAD//1024} KB), skipping")
                return None

        mime = ir.headers.get("content-type", "").split(";")[0].strip().lower() or "image/jpeg"
        if not mime.startswith("image/") or "svg" in mime:
            return None

        return (img_bytes, mime)
    except Exception as e:
        print(f"  IMG: ✗ fetch failed: {e}")
        return None


def prepare_image_for_bluesky(img_bytes: bytes, mime: str) -> tuple[bytes, str] | None:
    try:
        from PIL import Image
    except ImportError:
        return (img_bytes, mime) if len(img_bytes) <= IMG_TARGET_SIZE else None

    try:
        im = Image.open(io.BytesIO(img_bytes))
    except Exception as e:
        print(f"  IMG: ✗ Pillow could not open the image: {e}")
        return None

    if len(img_bytes) <= IMG_TARGET_SIZE and mime in ("image/jpeg", "image/png", "image/webp"):
        return (img_bytes, mime)

    if im.mode in ("RGBA", "LA", "P"):
        im = im.convert("RGB")

    max_side = 1600
    if max(im.size) > max_side:
        ratio = max_side / max(im.size)
        new_size = (int(im.size[0] * ratio), int(im.size[1] * ratio))
        im = im.resize(new_size, Image.Resampling.LANCZOS)

    for quality in (85, 75, 65, 55, 45):
        buf = io.BytesIO()
        im.save(buf, format="JPEG", quality=quality, optimize=True)
        data = buf.getvalue()
        if len(data) <= IMG_TARGET_SIZE:
            return (data, "image/jpeg")

    return None


# ---------------------------------------------------------------------------
# Summarization
# ---------------------------------------------------------------------------

def summarize(b: dict) -> str:
    abstract = (b["abstract"] or "").strip()
    title = b["title"].strip()
    fallback = abstract[:180] if (abstract and abstract.lower() != title.lower()) else ""

    if not ANTHROPIC_KEY:
        return fallback

    body_for_prompt = abstract if (abstract and abstract.lower() != title.lower()) else title

    prompt = (
        "You are summarizing a US legislative bill for a civic-engagement Bluesky bot "
        "that focuses on transportation. Write ONE plain-text sentence (under 180 "
        "characters) describing what the bill does, neutrally. No emoji, no hashtags, "
        "no editorializing, no quotes around the summary, no leading phrase like 'This "
        "bill'. Just the substance.\n\n"
        f"Title: {title}\n"
        f"Description: {body_for_prompt[:2000]}"
    )

    try:
        r = requests.post(
            ANTHROPIC_API,
            headers={"x-api-key": ANTHROPIC_KEY, "anthropic-version": "2023-06-01", "content-type": "application/json"},
            json={"model": ANTHROPIC_MODEL, "max_tokens": 200, "messages": [{"role": "user", "content": prompt}]},
            timeout=30,
        )
        if not r.ok:
            print(f"  ! Anthropic {r.status_code}: {r.text[:300]}", file=sys.stderr)
            r.raise_for_status()
        data = r.json()
        text = "".join(blk.get("text", "") for blk in data.get("content", []) if blk.get("type") == "text")
        return text.strip().strip('"').strip()
    except Exception as e:
        print(f"  ! summarization failed, using fallback: {e}", file=sys.stderr)
        return fallback


# ---------------------------------------------------------------------------
# Bluesky
# ---------------------------------------------------------------------------

class BlueskyClient:
    def __init__(self, handle: str, password: str):
        self.session = requests.Session()
        r = self.session.post(
            f"{BLUESKY_API}/com.atproto.server.createSession",
            json={"identifier": handle, "password": password},
            timeout=30,
        )
        r.raise_for_status()
        d = r.json()
        self.did = d["did"]
        self.session.headers["Authorization"] = f"Bearer {d['accessJwt']}"

    def upload_blob(self, data: bytes, mime: str) -> dict | None:
        try:
            r = self.session.post(
                f"{BLUESKY_API}/com.atproto.repo.uploadBlob",
                data=data,
                headers={"Content-Type": mime},
                timeout=30,
            )
            r.raise_for_status()
            return r.json().get("blob")
        except Exception as e:
            print(f"  - blob upload failed: {e}", file=sys.stderr)
            return None

    def post(self, text: str, link_url: str, embed_title: str, embed_desc: str,
             thumb_blob: dict | None = None) -> dict:
        record = {
            "$type": "app.bsky.feed.post",
            "text": text,
            "createdAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        }
        if link_url:
            external = {"uri": link_url, "title": embed_title[:300], "description": embed_desc[:1000]}
            if thumb_blob:
                external["thumb"] = thumb_blob
            record["embed"] = {"$type": "app.bsky.embed.external", "external": external}
            if link_url in text:
                tb = text.encode("utf-8")
                ub = link_url.encode("utf-8")
                start = tb.find(ub)
                if start >= 0:
                    record["facets"] = [{
                        "index": {"byteStart": start, "byteEnd": start + len(ub)},
                        "features": [{"$type": "app.bsky.richtext.facet#link", "uri": link_url}],
                    }]
        r = self.session.post(
            f"{BLUESKY_API}/com.atproto.repo.createRecord",
            json={"repo": self.did, "collection": "app.bsky.feed.post", "record": record},
            timeout=30,
        )
        r.raise_for_status()
        return r.json()


# ---------------------------------------------------------------------------
# Universal Open States link builder (replaces 8 per-state builders)
# ---------------------------------------------------------------------------

def _normalize_session_for_openstates(session: str) -> str:
    """
    Open States' URL slugs match what govbot's `legislative_session` field
    contains. Empirically:
      - IL '104th'      -> '104th'
      - IN '2026'       -> '2026'
      - IA '2025-2026'  -> '2025-2026'
      - MI '2025-2026'  -> '2025-2026'
      - MN '2025s1'     -> '2025s1'
      - MO '2025S2'     -> '2025S2'
      - GA  -> '2025_26' (Georgia uses underscores)
      - CT  -> '2025'
    Govbot tends to mirror Open States' session identifiers, so we pass
    through as-is. Govbot might canonicalize differently for some states,
    but as long as the same slug is consistent across both, this works.
    """
    return (session or "").strip()


def _normalize_identifier_for_openstates(ident: str) -> str:
    """Open States URLs use the identifier with no internal spaces: 'HB 1032' -> 'HB1032'."""
    return re.sub(r"\s+", "", ident or "")


def link_for(b: dict) -> str:
    """
    Build an Open States bill URL.
    Pattern: https://openstates.org/{state}/bills/{session}/{identifier}/
    Works for all 50 states + DC + PR (all jurisdictions Open States supports).
    """
    state = (b.get("state") or "").lower()
    session = _normalize_session_for_openstates(b.get("session", ""))
    identifier = _normalize_identifier_for_openstates(b.get("identifier", ""))
    if not (state and session and identifier):
        return ""
    return f"https://openstates.org/{state}/bills/{session}/{identifier}/"


# ---------------------------------------------------------------------------
# Composition
# ---------------------------------------------------------------------------

def emoji_for(b: dict) -> str:
    s = " ".join([b["title"], b["abstract"], b["subjects"]]).lower()
    if any(w in s for w in ("transit", "school bus", "bus driver", "subway")): return "🚌"
    if any(w in s for w in ("rail", "amtrak", "railroad", "railway", "metra")): return "🚆"
    if any(w in s for w in ("airport", "aviation", "aircraft")):                return "✈️"
    if any(w in s for w in ("bicycle", "bike lane", "cyclist")):                return "🚲"
    if any(w in s for w in ("pedestrian", "sidewalk", "crosswalk")):            return "🚶"
    if any(w in s for w in ("electric vehicle", "ev charging")):                return "🔌"
    if any(w in s for w in ("highway", "tollway", "expressway", "interstate")): return "🛣️"
    if any(w in s for w in ("truck", "freight", "commercial vehicle")):         return "🚛"
    return "🚗"


def compose_post(b: dict, summary: str) -> tuple[str, str, str, str]:
    emoji = emoji_for(b)
    link = link_for(b)
    link_block = f"\n\n{LINK_PREFIX}{link}" if link else ""

    state_label = b["state"] or "?"
    display = best_display_text(b).strip()
    summary = (summary or "").strip()

    summary_block = f"\n\n{summary}" if (summary and summary.lower() != display.lower()) else ""
    action_line = format_action_line(b["action_desc"], b["action_date"])
    action_block = f"\n\n{action_line}" if action_line else ""

    head = f"{emoji} {state_label} {b['identifier']} — {display}"

    def assemble(h, s, a, l):
        return h + s + a + l

    text = assemble(head, summary_block, action_block, link_block)

    if len(text) > MAX_POST and summary_block:
        overflow = len(text) - MAX_POST
        new_len = max(0, len(summary) - overflow - 1)
        if new_len > 20:
            summary = summary[:new_len].rstrip() + "…"
            summary_block = f"\n\n{summary}"
        else:
            summary_block = ""
        text = assemble(head, summary_block, action_block, link_block)

    if len(text) > MAX_POST and action_block and action_line:
        nice_date = _format_date(b["action_date"])
        if nice_date:
            date_prefix = f"{nice_date}: "
            if action_line.startswith(date_prefix):
                desc_part = action_line[len(date_prefix):].rstrip(".!?")
                overflow = len(text) - MAX_POST
                new_len = max(0, len(desc_part) - overflow - 1)
                if new_len > 8:
                    action_line = date_prefix + desc_part[:new_len].rstrip() + "…"
                else:
                    action_line = nice_date
            action_block = f"\n\n{action_line}"
        text = assemble(head, summary_block, action_block, link_block)

    if len(text) > MAX_POST:
        avail = MAX_POST - len(link_block) - len(summary_block) - len(action_block) \
                - len(emoji) - len(f" {state_label} {b['identifier']} — ") - 1
        display_trimmed = display[:max(0, avail)].rstrip() + "…"
        head = f"{emoji} {state_label} {b['identifier']} — {display_trimmed}"
        text = assemble(head, summary_block, action_block, link_block)

    state_name = STATE_FULL_NAME.get(b["state"], b["state"] or "Bill")
    embed_title = f"{state_name} {b['identifier']}"[:300]
    embed_desc = (b["abstract"] or summary or display)[:280]
    return text, link, embed_title, embed_desc


# ---------------------------------------------------------------------------
# State persistence
# ---------------------------------------------------------------------------

def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except json.JSONDecodeError:
            pass
    return {"posted": []}


def save_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2))


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    if not DRY_RUN and (not BSKY_HANDLE or not BSKY_PASSWORD):
        print("ERROR: BLUESKY_HANDLE and BLUESKY_APP_PASSWORD must be set.", file=sys.stderr)
        return 1

    records = load_bills(JSONL_PATH)
    if not records:
        return 0

    state = load_state()
    seen = set(state.get("posted", []))

    candidates: list[dict] = []
    for r in records:
        b = extract_fields(r)
        if not b:
            continue
        if not is_transportation(b):
            continue
        if b["dedup_key"] in seen:
            continue
        candidates.append(b)

    # Same-day dedup (collapse multiple log entries for same bill on same day).
    unique_by_day: dict[str, dict] = {}
    for b in candidates:
        existing = unique_by_day.get(b["same_day_key"])
        if existing is None or len(b["action_desc"]) > len(existing["action_desc"]):
            unique_by_day[b["same_day_key"]] = b
    candidates = list(unique_by_day.values())

    print(f"Found {len(candidates)} new transportation-related bill update(s).")
    if not candidates:
        return 0

    # Print a state-distribution summary so we can see coverage.
    from collections import Counter
    state_counts = Counter(b["state"] or "?" for b in candidates)
    top = state_counts.most_common(15)
    print(f"  by state: {', '.join(f'{s}={n}' for s,n in top)}")

    def sort_key(b: dict):
        try:
            return datetime.strptime(b["action_date"], "%Y-%m-%d")
        except (ValueError, TypeError):
            return datetime.min

    candidates.sort(key=sort_key, reverse=True)
    to_post = candidates[:POST_LIMIT]
    print(f"Will post up to {POST_LIMIT}: posting {len(to_post)}.")

    client = None if DRY_RUN else BlueskyClient(BSKY_HANDLE, BSKY_PASSWORD)

    for b in to_post:
        summary = summarize(b)
        text, link, ec_title, ec_desc = compose_post(b, summary)

        thumb_blob = None
        if link:
            print(f"  IMG: fetching og:image for {link}")
            fetched = fetch_og_image(link)
            if fetched:
                img_bytes_raw, mime_raw = fetched
                print(f"  IMG: downloaded {len(img_bytes_raw)//1024} KB ({mime_raw})")
                prepared = prepare_image_for_bluesky(img_bytes_raw, mime_raw)
                if prepared:
                    img_bytes, img_mime = prepared
                    if client:
                        thumb_blob = client.upload_blob(img_bytes, img_mime)
                        if thumb_blob:
                            print(f"  IMG: ✓ attached ({len(img_bytes)//1024} KB, {img_mime})")
                        else:
                            print(f"  IMG: ✗ blob upload failed")
                    else:
                        print(f"  IMG: [dry-run] would attach ({len(img_bytes)//1024} KB)")
                else:
                    print(f"  IMG: ✗ couldn't fit under size cap")
            else:
                print(f"  IMG: ✗ no usable og:image found")

        print(f"\n--- {b['state'] or '?'} {b['identifier']} ({b['action_date']}) ---")
        print(f"    same_day_key: {b['same_day_key']}")
        print(text)
        print("---")

        if client:
            try:
                client.post(text, link, ec_title, ec_desc, thumb_blob=thumb_blob)
                time.sleep(2)
            except requests.HTTPError as e:
                print(f"  ! post failed: {e.response.status_code} {e.response.text}", file=sys.stderr)
                continue

        seen.add(b["dedup_key"])

    state["posted"] = sorted(seen)
    state["last_run"] = datetime.now(timezone.utc).isoformat()
    save_state(state)
    print(f"\nDone. State saved to {STATE_FILE.relative_to(ROOT)}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
