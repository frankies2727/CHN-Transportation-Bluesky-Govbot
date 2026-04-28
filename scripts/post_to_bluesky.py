#!/usr/bin/env python3
"""
Filter govbot's bills.jsonl for transportation-related bills across multiple
states, dedupe against state/posted.json, summarize with Claude, and post to
Bluesky.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
JSONL_PATH = ROOT / "bills.jsonl"
STATE_FILE = ROOT / "state" / "posted.json"

POST_LIMIT = int(os.environ.get("POST_LIMIT", "2"))
DRY_RUN = os.environ.get("DRY_RUN") == "1"

BSKY_HANDLE = os.environ.get("BLUESKY_HANDLE", "")
BSKY_PASSWORD = os.environ.get("BLUESKY_APP_PASSWORD", "")
ANTHROPIC_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

BLUESKY_API = "https://bsky.social/xrpc"
ANTHROPIC_API = "https://api.anthropic.com/v1/messages"
ANTHROPIC_MODEL = "claude-haiku-4-5-20251001"

US_STATES = {
    "AL","AK","AZ","AR","CA","CO","CT","DE","FL","GA","HI","ID","IL","IN","IA",
    "KS","KY","LA","ME","MD","MA","MI","MN","MS","MO","MT","NE","NV","NH","NJ",
    "NM","NY","NC","ND","OH","OK","OR","PA","RI","SC","SD","TN","TX","UT","VT",
    "VA","WA","WV","WI","WY","DC","PR","GU","VI","AS","MP",
}

# Map 2-letter state codes to full names for the link-card title.
STATE_FULL_NAME = {
    "IL": "Illinois", "IN": "Indiana", "IA": "Iowa", "MI": "Michigan",
    "MN": "Minnesota", "MO": "Missouri", "OH": "Ohio", "WI": "Wisconsin",
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas",
    "CA": "California", "CO": "Colorado", "CT": "Connecticut", "DE": "Delaware",
    "FL": "Florida", "GA": "Georgia", "HI": "Hawaii", "ID": "Idaho",
    "KS": "Kansas", "KY": "Kentucky", "LA": "Louisiana", "ME": "Maine",
    "MD": "Maryland", "MA": "Massachusetts", "MS": "Mississippi", "MT": "Montana",
    "NE": "Nebraska", "NV": "Nevada", "NH": "New Hampshire", "NJ": "New Jersey",
    "NM": "New Mexico", "NY": "New York", "NC": "North Carolina", "ND": "North Dakota",
    "OK": "Oklahoma", "OR": "Oregon", "PA": "Pennsylvania", "RI": "Rhode Island",
    "SC": "South Carolina", "SD": "South Dakota", "TN": "Tennessee", "TX": "Texas",
    "UT": "Utah", "VT": "Vermont", "VA": "Virginia", "WA": "Washington",
    "WV": "West Virginia", "WY": "Wyoming", "DC": "Washington D.C.",
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
    """'2025-05-08' -> 'May 8, 2025'. Uses a period after abbreviations
    (e.g. 'Nov.', 'Sept.') for months that are commonly abbreviated.
    May, June, and July are short enough to spell out in full."""
    try:
        d = datetime.strptime(yyyy_mm_dd, "%Y-%m-%d")
    except ValueError:
        return ""
    # AP-style month abbreviations
    abbrev = {1:"Jan.", 2:"Feb.", 3:"March", 4:"April", 5:"May", 6:"June",
              7:"July", 8:"Aug.", 9:"Sept.", 10:"Oct.", 11:"Nov.", 12:"Dec."}
    return f"{abbrev[d.month]} {d.day}, {d.year}"


def _smart_case(s: str) -> str:
    """Convert SHOUTY ALL-CAPS to Title Case; capitalize first letter of
    other strings; leave already-mixed-case alone otherwise."""
    s = s.strip().rstrip(".")
    if not s:
        return s
    letters = [c for c in s if c.isalpha()]
    if letters and sum(1 for c in letters if c.isupper()) / len(letters) > 0.7:
        # All-caps -> Title Case (with small words lowercase except the first)
        small = {"a","an","and","of","or","the","to","by","in","on","for","with","at"}
        words = s.lower().split()
        out = []
        for i, w in enumerate(words):
            out.append(w.capitalize() if (i == 0 or w not in small) else w)
        return " ".join(out)
    # Otherwise, just ensure the first character is capitalized.
    return s[0].upper() + s[1:] if s[0].isalpha() else s


def format_action_line(action_desc: str, date_yyyy_mm_dd: str) -> str:
    """Produces 'Nov. 4, 2025: Placed on third reading.' or partial variants."""
    desc = _smart_case(action_desc)
    nice_date = _format_date(date_yyyy_mm_dd)
    if desc and nice_date:
        # Make sure the description ends with a period.
        desc_with_period = desc if desc.endswith((".", "!", "?")) else desc + "."
        return f"{nice_date}: {desc_with_period}"
    if nice_date:
        return f"Latest action: {nice_date}"
    if desc:
        return desc
    return ""


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

    def post(self, text: str, link_url: str, embed_title: str, embed_desc: str) -> dict:
        record = {
            "$type": "app.bsky.feed.post",
            "text": text,
            "createdAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        }
        if link_url:
            record["embed"] = {
                "$type": "app.bsky.embed.external",
                "external": {"uri": link_url, "title": embed_title[:300], "description": embed_desc[:1000]},
            }
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
# Per-state link builders
# ---------------------------------------------------------------------------

def _split_identifier(ident: str) -> tuple[str, str]:
    m = re.match(r"^\s*([A-Za-z]+)\s*0*(\d+)\s*$", ident or "")
    if not m:
        return "", ""
    return m.group(1).upper(), m.group(2)


def _ga_from_session(session: str) -> str:
    m = re.match(r"^\s*(\d+)", session or "")
    return m.group(1) if m else ""


def _year_from_session(session: str, default: str = "2025") -> str:
    m = re.search(r"(20\d{2})", session or "")
    return m.group(1) if m else default


def link_il(ident, session):
    p, n = _split_identifier(ident)
    if not (p and n): return ""
    ga = _ga_from_session(session) or "104"
    return f"https://www.ilga.gov/legislation/billstatus.asp?DocNum={n}&GAID={ga}&GA={ga}&DocTypeID={p}"

def link_mi(ident, session):
    p, n = _split_identifier(ident)
    if not (p and n): return ""
    return f"https://www.legislature.mi.gov/Search/Bills?bills={p}-{n}"

def link_in(ident, session):
    p, n = _split_identifier(ident)
    if not (p and n): return ""
    year = _year_from_session(session, "2026")
    chamber = "house" if p.startswith("H") else "senate"
    return f"https://iga.in.gov/legislative/{year}/bills/{chamber}/{n}"

def link_ia(ident, session):
    p, n = _split_identifier(ident)
    if not (p and n): return ""
    return f"https://www.legis.iowa.gov/legislation/BillBook?ga=91&ba={p}{n}"

def link_oh(ident, session):
    p, n = _split_identifier(ident)
    if not (p and n): return ""
    chamber = "house" if p.startswith("H") else "senate"
    return f"https://www.legislature.ohio.gov/legislation/{chamber}-bill/{n}"

def link_wi(ident, session):
    p, n = _split_identifier(ident)
    if not (p and n): return ""
    year = _year_from_session(session, "2025")
    return f"https://docs.legis.wisconsin.gov/{year}/proposals/{p.lower()}{n}"

def link_mn(ident, session):
    p, n = _split_identifier(ident)
    if not (p and n): return ""
    year = _year_from_session(session, "2025")
    chamber = "House" if p.startswith("H") else "Senate"
    return f"https://www.revisor.mn.gov/bills/bill.php?b={chamber}&f={p}{n}&y={year}"

def link_mo(ident, session):
    p, n = _split_identifier(ident)
    if not (p and n): return ""
    chamber = "house" if p.startswith("H") else "senate"
    return f"https://www.{chamber}.mo.gov/Bill.aspx?bill={p}{n}"


STATE_LINK_BUILDERS = {
    "IL": link_il, "MI": link_mi, "IN": link_in, "IA": link_ia,
    "OH": link_oh, "WI": link_wi, "MN": link_mn, "MO": link_mo,
}


def link_for(b):
    builder = STATE_LINK_BUILDERS.get(b.get("state", ""))
    return builder(b["identifier"], b.get("session", "")) if builder else ""


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
    """
    Layout:
        <emoji> <STATE> <ID> — <title or abstract>

        <one-sentence summary>      ← skipped if absent or duplicates head

        <action description on Month D, YYYY>   ← skipped if no action info

        🔗 <link>                   ← skipped if no link
    """
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
            # Pull out the description portion (everything after the prefix)
            if action_line.startswith(date_prefix):
                desc_part = action_line[len(date_prefix):].rstrip(".!?")
                overflow = len(text) - MAX_POST
                new_len = max(0, len(desc_part) - overflow - 1)
                if new_len > 8:
                    action_line = date_prefix + desc_part[:new_len].rstrip() + "…"
                else:
                    action_line = nice_date  # fall back to just the date
            action_block = f"\n\n{action_line}"
        text = assemble(head, summary_block, action_block, link_block)

    if len(text) > MAX_POST:
        avail = MAX_POST - len(link_block) - len(summary_block) - len(action_block) \
                - len(emoji) - len(f" {state_label} {b['identifier']} — ") - 1
        display_trimmed = display[:max(0, avail)].rstrip() + "…"
        head = f"{emoji} {state_label} {b['identifier']} — {display_trimmed}"
        text = assemble(head, summary_block, action_block, link_block)

    # --- Link card content ---
    # Title is just "<State Name> <ID>" so it doesn't echo the post text.
    state_name = STATE_FULL_NAME.get(b["state"], b["state"] or "Bill")
    embed_title = f"{state_name} {b['identifier']}"[:300]

    # Description = abstract (preferred) or summary (fallback).
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

    unique_by_key: dict[str, dict] = {}
    for b in candidates:
        unique_by_key.setdefault(b["dedup_key"], b)
    candidates = list(unique_by_key.values())

    print(f"Found {len(candidates)} new transportation-related bill update(s).")
    if not candidates:
        return 0

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
        print(f"\n--- {b['state'] or '?'} {b['identifier']} ({b['action_date']}) ---")
        print(text)
        print(f"[card title: {ec_title!r}]")
        print(f"[card desc:  {ec_desc[:80]!r}…]")
        print("---")

        if client:
            try:
                client.post(text, link, ec_title, ec_desc)
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
