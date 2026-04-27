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

POST_LIMIT = int(os.environ.get("POST_LIMIT", "3"))
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

# Map full state names to codes — useful when the data only includes a name.
STATE_NAME_TO_CODE = {
    "alabama":"AL","alaska":"AK","arizona":"AZ","arkansas":"AR","california":"CA",
    "colorado":"CO","connecticut":"CT","delaware":"DE","florida":"FL","georgia":"GA",
    "hawaii":"HI","idaho":"ID","illinois":"IL","indiana":"IN","iowa":"IA",
    "kansas":"KS","kentucky":"KY","louisiana":"LA","maine":"ME","maryland":"MD",
    "massachusetts":"MA","michigan":"MI","minnesota":"MN","mississippi":"MS",
    "missouri":"MO","montana":"MT","nebraska":"NE","nevada":"NV","new hampshire":"NH",
    "new jersey":"NJ","new mexico":"NM","new york":"NY","north carolina":"NC",
    "north dakota":"ND","ohio":"OH","oklahoma":"OK","oregon":"OR","pennsylvania":"PA",
    "rhode island":"RI","south carolina":"SC","south dakota":"SD","tennessee":"TN",
    "texas":"TX","utah":"UT","vermont":"VT","virginia":"VA","washington":"WA",
    "west virginia":"WV","wisconsin":"WI","wyoming":"WY",
    "district of columbia":"DC",
}

TRANSPORTATION_KEYWORDS = [
    "transportation", "transit", "rail", "railroad", "railway",
    "subway", "streetcar", "light rail", "commuter rail", "ferry",
    "amtrak", "metra", "cta", "pace", "rta", "idot", "indot", "modot", "wsdot", "mdot",
    "bicycle", "bicyclist", "bike lane", "cyclist",
    "pedestrian", "sidewalk", "crosswalk", "walkability",
    "airport", "aviation", "airline",
    "freight", "trucking", "commercial vehicle", "cdl",
    "rideshare", "ride-share", "ride share", "taxicab",
    "ev charging", "electric vehicle", "autonomous vehicle",
    "scooter", "e-bike",
    "highway", "tollway", "roadway", "expressway", "interstate",
    "tollbooth", "toll road", "toll bridge",
    "traffic signal", "traffic safety", "road construction",
    "complete streets", "vision zero", "pedestrian safety",
    "infrastructure", "transportation infrastructure",
    "motor vehicle", "motor fuel tax", "gas tax",
    "vehicle registration", "license plate", "driver's license",
    "speed limit", "dui", "dwi", "seatbelt", "helmet law",
    "auto insurance",
    "traffic", "parking", "congestion", "mobility",
]

_KEYWORD_PATTERN = re.compile(
    r"\b(" + "|".join(re.escape(k) for k in TRANSPORTATION_KEYWORDS) + r")\b",
    re.IGNORECASE,
)

MAX_POST = 290
LINK_PREFIX = "🔗 "


# ---------------------------------------------------------------------------
# Loading & state detection
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


# Known state-legislature domain prefixes. Add more as you expand coverage.
DOMAIN_TO_STATE = {
    "ilga.gov": "IL",
    "iga.in.gov": "IN",
    "legislature.mi.gov": "MI",
    "docs.legis.wisconsin.gov": "WI",
    "legis.wisconsin.gov": "WI",
    "ohiosenate.gov": "OH",
    "ohiohouse.gov": "OH",
    "legislature.ohio.gov": "OH",
    "legis.iowa.gov": "IA",
    "house.mo.gov": "MO",
    "senate.mo.gov": "MO",
    "revisor.mo.gov": "MO",
    "revisor.mn.gov": "MN",
    "leg.mn.gov": "MN",
    "house.mn.gov": "MN",
    "senate.mn": "MN",
}


def _state_from_text(text: str) -> str:
    """Try to extract a 2-letter state code from a string of any shape."""
    if not isinstance(text, str):
        return ""

    # Pattern 0: known legislature domain
    lower_text = text.lower()
    for domain, code in DOMAIN_TO_STATE.items():
        if domain in lower_text:
            return code

    # Pattern 1: 2-letter code surrounded by non-letters (handles 'IL', 'us-il', '/il/')
    for m in re.finditer(r"(?<![A-Za-z])([A-Za-z]{2})(?![A-Za-z])", text):
        code = m.group(1).upper()
        if code in US_STATES:
            return code

    # Pattern 2: full state name (case-insensitive)
    lower = text.lower()
    for name, code in STATE_NAME_TO_CODE.items():
        # Use word boundaries to avoid 'Indiana' matching 'Indianapolis' incorrectly
        if re.search(r"\b" + re.escape(name) + r"\b", lower):
            return code

    return ""


def detect_state(record: dict) -> str:
    """Find the state code by checking many spots in the record."""
    # 1) Top-level sources URLs (most reliable)
    sources = record.get("sources") or []
    if isinstance(sources, list):
        for s in sources:
            if isinstance(s, dict):
                for v in s.values():
                    code = _state_from_text(v if isinstance(v, str) else "")
                    if code:
                        return code
            elif isinstance(s, str):
                code = _state_from_text(s)
                if code:
                    return code

    # 2) bill.from_organization (often "Michigan Senate" or similar)
    bill = record.get("bill") or {}
    org = bill.get("from_organization")
    if isinstance(org, dict):
        for v in org.values():
            code = _state_from_text(v if isinstance(v, str) else "")
            if code:
                return code
    elif isinstance(org, str):
        code = _state_from_text(org)
        if code:
            return code

    # 3) bill.legislative_session (might mention the state)
    sess = bill.get("legislative_session")
    if isinstance(sess, dict):
        for v in sess.values():
            code = _state_from_text(v if isinstance(v, str) else "")
            if code:
                return code
    elif isinstance(sess, str):
        code = _state_from_text(sess)
        if code:
            return code

    # 4) Record id (e.g., "il-2025-SB857")
    rid = record.get("id") or ""
    m = re.match(r"^([a-z]{2})[-_/]", rid, re.IGNORECASE)
    if m and m.group(1).upper() in US_STATES:
        return m.group(1).upper()

    # 5) Anywhere else with `jurisdiction`/`state`/`scope`
    for container in (record, bill):
        for key in ("jurisdiction", "state", "locale", "scope"):
            v = container.get(key)
            if isinstance(v, str):
                code = _state_from_text(v)
                if code:
                    return code
            elif isinstance(v, dict):
                for sub in v.values():
                    code = _state_from_text(sub if isinstance(sub, str) else "")
                    if code:
                        return code

    return ""


def find_url_in_record(record: dict) -> str:
    """Return the first usable source URL anywhere in the record."""
    # Top-level sources is the canonical place
    sources = record.get("sources") or []
    if isinstance(sources, list):
        for s in sources:
            url = _extract_url_from(s)
            if url:
                return url

    bill = record.get("bill") or {}
    for collection_name in ("sources", "links", "versions", "documents"):
        items = bill.get(collection_name) or []
        if isinstance(items, list):
            for item in items:
                url = _extract_url_from(item)
                if url:
                    return url

    log = record.get("log") or {}
    action = log.get("action") or {}
    url = _extract_url_from(action)
    if url:
        return url

    for key in ("url", "uri", "link", "openstates_url"):
        v = record.get(key) or bill.get(key)
        if isinstance(v, str) and v.startswith("http"):
            return v

    return ""


def _extract_url_from(obj) -> str:
    if isinstance(obj, str) and obj.startswith("http"):
        return obj
    if isinstance(obj, dict):
        for key in ("url", "uri", "link", "href"):
            v = obj.get(key)
            if isinstance(v, str) and v.startswith("http"):
                return v
    return ""


def extract_fields(record: dict) -> dict | None:
    bill = record.get("bill") or {}
    log = record.get("log") or {}

    identifier = bill.get("identifier") or record.get("id") or ""
    title = bill.get("title") or ""
    if not identifier or not title:
        return None

    state = detect_state(record)
    record_url = find_url_in_record(record)

    abstracts = bill.get("abstracts") or []
    abstract = ""
    if abstracts and isinstance(abstracts, list):
        first = abstracts[0]
        if isinstance(first, dict):
            abstract = first.get("abstract", "")
        elif isinstance(first, str):
            abstract = first

    sponsors_list = bill.get("sponsors") or log.get("sponsors") or []
    sponsor = ""
    if sponsors_list and isinstance(sponsors_list, list):
        first_sp = sponsors_list[0]
        if isinstance(first_sp, dict):
            sponsor = first_sp.get("name", "")
        elif isinstance(first_sp, str):
            sponsor = first_sp

    action = log.get("action") or {}
    action_desc = action.get("description") or ""
    action_date = action.get("date") or ""

    dedup_key = f"{state}|{identifier}|{action_date}|{action_desc[:40]}"

    return {
        "state": state,
        "identifier": identifier,
        "title": title,
        "abstract": abstract,
        "sponsor": sponsor,
        "action_desc": action_desc,
        "action_date": action_date,
        "record_url": record_url,
        "dedup_key": dedup_key,
    }


def is_transportation(b: dict) -> bool:
    haystack = " ".join([b["title"], b["abstract"], b["action_desc"]]).lower()
    return bool(_KEYWORD_PATTERN.search(haystack))


# ---------------------------------------------------------------------------
# Summarization
# ---------------------------------------------------------------------------

def summarize(b: dict) -> str:
    abstract = (b["abstract"] or "").strip()
    title = b["title"].strip()

    # If abstract is empty or duplicates the title, fallback is just empty
    # (we'll show the title alone in compose_post).
    if not abstract or abstract.lower() == title.lower():
        fallback = ""
    else:
        fallback = abstract[:180]

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
            headers={
                "x-api-key": ANTHROPIC_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": ANTHROPIC_MODEL,
                "max_tokens": 200,
                "messages": [{"role": "user", "content": prompt}],
            },
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
                "external": {
                    "uri": link_url,
                    "title": embed_title[:300],
                    "description": embed_desc[:1000],
                },
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
            json={
                "repo": self.did,
                "collection": "app.bsky.feed.post",
                "record": record,
            },
            timeout=30,
        )
        r.raise_for_status()
        return r.json()


# ---------------------------------------------------------------------------
# Composition
# ---------------------------------------------------------------------------

def ilga_link(identifier: str) -> str:
    m = re.match(r"^\s*([HhSs][BbRrJjMm]?)\s*0*(\d+)\s*$", identifier or "")
    if not m:
        return ""
    return f"https://www.ilga.gov/legislation/billstatus.asp?DocNum={m.group(2)}&GAID=17&GA=104&DocTypeID={m.group(1).upper()}"


def link_for(b: dict) -> str:
    if b.get("record_url"):
        return b["record_url"]
    if b.get("state") == "IL":
        return ilga_link(b["identifier"])
    return ""


def emoji_for(title: str, abstract: str) -> str:
    s = (title + " " + abstract).lower()
    if any(w in s for w in ("transit", "cta", "metra", "pace", "bus", "subway")): return "🚇"
    if any(w in s for w in ("rail", "amtrak", "railroad", "railway")):           return "🚆"
    if any(w in s for w in ("airport", "aviation")):                             return "✈️"
    if any(w in s for w in ("bicycle", "bike lane", "cyclist")):                 return "🚲"
    if any(w in s for w in ("pedestrian", "sidewalk", "crosswalk")):             return "🚶"
    if any(w in s for w in ("electric vehicle", "ev charging")):                 return "🔌"
    if any(w in s for w in ("highway", "tollway", "expressway", "interstate")):  return "🛣️"
    if any(w in s for w in ("truck", "freight", "commercial vehicle")):          return "🚛"
    return "🚗"


def compose_post(b: dict, summary: str) -> tuple[str, str, str, str]:
    emoji = emoji_for(b["title"], b["abstract"])
    link = link_for(b)
    link_block = f"\n\n{LINK_PREFIX}{link}" if link else ""

    state_label = b["state"] or "?"
    title = b["title"].strip()
    summary = (summary or "").strip()

    # Skip the summary line if it's empty or just duplicates the title.
    if not summary or summary.lower() == title.lower():
        body_extra = ""
    else:
        body_extra = f"\n\n{summary}"

    head = f"{emoji} {state_label} {b['identifier']} — {title}"
    body = head + body_extra

    if len(body + link_block) > MAX_POST:
        if body_extra:
            overflow = len(body + link_block) - MAX_POST
            new_len = max(0, len(summary) - overflow - 1)
            if new_len > 20:
                summary = summary[:new_len].rstrip() + "…"
                body_extra = f"\n\n{summary}"
                body = head + body_extra
        if len(body + link_block) > MAX_POST:
            avail = MAX_POST - len(link_block) - len(emoji) - len(f" {state_label} {b['identifier']} — ") - 1
            title = title[:max(0, avail)].rstrip() + "…"
            head = f"{emoji} {state_label} {b['identifier']} — {title}"
            body = head + body_extra

    text = body + link_block
    embed_title = f"{state_label} {b['identifier']}: {b['title']}"[:300]
    embed_desc = b["abstract"][:280] if b["abstract"] else summary
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

    # Deeper debug: show one full sample record so we can see what's actually in there.
    if records:
        sample = records[0]
        print("=" * 60)
        print("DEBUG sample record (first 1500 chars):")
        print(json.dumps(sample, indent=2)[:1500])
        print("=" * 60)

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
