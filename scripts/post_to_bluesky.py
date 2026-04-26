#!/usr/bin/env python3
"""
Filter govbot's bills.jsonl for transportation-related Illinois bills,
dedupe against state/posted.json, summarize with Claude, and post to Bluesky.

Env vars (required at post time):
    BLUESKY_HANDLE
    BLUESKY_APP_PASSWORD
    ANTHROPIC_API_KEY     (optional — falls back to abstract if missing)

Optional:
    POST_LIMIT            max posts per run (default 3)
    DRY_RUN               if "1", print what would be posted but don't post
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
ANTHROPIC_MODEL = "claude-haiku-4-5"  # fast + cheap

# ---------------------------------------------------------------------------
# Transportation keywords. Whole-word matching, case-insensitive.
# Add/remove as you like — these are conservative defaults focused on Illinois.
# ---------------------------------------------------------------------------
TRANSPORTATION_KEYWORDS = [
    "transportation", "transit", "highway", "tollway", "roadway",
    "expressway", "interstate", "bridge", "rail", "railroad", "railway",
    "amtrak", "metra", "cta", "pace", "rta", "idot",
    "bicycle", "bike lane", "pedestrian", "sidewalk", "crosswalk",
    "vehicle", "automobile", "motor vehicle", "driver", "license plate",
    "traffic", "speed limit", "tollbooth", "toll road", "parking",
    "airport", "aviation", "freight",
    "ev charging", "electric vehicle", "autonomous vehicle",
]

# Compile once. \b is word boundary; we lowercase both sides.
_KEYWORD_PATTERN = re.compile(
    r"\b(" + "|".join(re.escape(k) for k in TRANSPORTATION_KEYWORDS) + r")\b",
    re.IGNORECASE,
)

MAX_POST = 290  # Bluesky's hard limit is 300 graphemes; stay under.


# ---------------------------------------------------------------------------
# Loading & normalizing bills
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


def extract_fields(record: dict) -> dict | None:
    """
    Pull the fields we care about out of a govbot log record. Returns None if
    the record doesn't look like a bill log entry we can use.
    """
    bill = record.get("bill") or {}
    log = record.get("log") or {}

    identifier = bill.get("identifier") or record.get("id") or ""
    title = bill.get("title") or ""
    if not identifier or not title:
        return None

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

    # A unique, stable id for dedup. Combine bill identifier + action date + a
    # truncated description hash so we don't keep posting the same bill every
    # time it gets a new committee read. If you'd rather post each bill ONCE
    # ever, change this to just `identifier`.
    dedup_key = f"{identifier}|{action_date}|{action_desc[:40]}"

    return {
        "identifier": identifier,
        "title": title,
        "abstract": abstract,
        "sponsor": sponsor,
        "action_desc": action_desc,
        "action_date": action_date,
        "dedup_key": dedup_key,
    }


def is_transportation(b: dict) -> bool:
    haystack = " ".join([b["title"], b["abstract"], b["action_desc"]]).lower()
    return bool(_KEYWORD_PATTERN.search(haystack))


# ---------------------------------------------------------------------------
# Summarization
# ---------------------------------------------------------------------------

def summarize(b: dict) -> str:
    """One neutral sentence under ~180 chars. Falls back to truncated abstract."""
    abstract = b["abstract"].strip()
    if not ANTHROPIC_KEY:
        return abstract[:180] if abstract else b["title"][:180]

    prompt = (
        "You are summarizing a US legislative bill for a civic-engagement Bluesky bot "
        "that focuses on transportation. Write ONE plain-text sentence (under 180 "
        "characters) describing what the bill does, neutrally. No emoji, no hashtags, "
        "no editorializing, no quotes around the summary, no leading phrase like 'This "
        "bill'. Just the substance.\n\n"
        f"Title: {b['title']}\n"
        f"Abstract: {abstract[:2000]}"
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
        r.raise_for_status()
        data = r.json()
        text = "".join(blk.get("text", "") for blk in data.get("content", []) if blk.get("type") == "text")
        return text.strip().strip('"').strip()
    except Exception as e:
        print(f"  ! summarization failed, using abstract: {e}", file=sys.stderr)
        return (abstract or b["title"])[:180]


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
            "embed": {
                "$type": "app.bsky.embed.external",
                "external": {
                    "uri": link_url,
                    "title": embed_title[:300],
                    "description": embed_desc[:1000],
                },
            },
        }

        # Make the URL within `text` a clickable facet (UTF-8 byte offsets).
        if link_url and link_url in text:
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
    """Build an ILGA bill-status URL from an identifier like 'HB1234' or 'SB567'."""
    m = re.match(r"^\s*([HhSs][BbRrJjMm]?)\s*0*(\d+)\s*$", identifier or "")
    if not m:
        return ""
    prefix = m.group(1).upper()
    num = m.group(2)
    # Common cases. ILGA's URL scheme: DocTypeID is HB/SB/HR/SR/etc.
    return f"https://www.ilga.gov/legislation/billstatus.asp?DocNum={num}&GAID=17&GA=104&DocTypeID={prefix}"


def emoji_for(title: str, abstract: str) -> str:
    s = (title + " " + abstract).lower()
    if any(w in s for w in ("transit", "cta", "metra", "pace", "bus", "subway")): return "🚇"
    if any(w in s for w in ("rail", "amtrak", "railroad", "railway")):           return "🚆"
    if any(w in s for w in ("airport", "aviation")):                             return "✈️"
    if any(w in s for w in ("bicycle", "bike lane")):                            return "🚲"
    if any(w in s for w in ("pedestrian", "sidewalk", "crosswalk")):             return "🚶"
    if any(w in s for w in ("electric vehicle", "ev charging")):                 return "🔌"
    if any(w in s for w in ("highway", "tollway", "expressway", "interstate")):  return "🛣️"
    return "🚗"


def compose_post(b: dict, summary: str) -> tuple[str, str, str, str]:
    """
    Returns (text, link_url, embed_title, embed_description).
    Text layout (under MAX_POST chars including the link):
        <emoji> IL <ID> — <Title>

        <summary>

        <link>
    """
    emoji = emoji_for(b["title"], b["abstract"])
    link = ilga_link(b["identifier"])
    link_block = f"\n\n{link}" if link else ""

    title = b["title"].strip()
    head = f"{emoji} IL {b['identifier']} — {title}"

    body = f"{head}\n\n{summary.strip()}"
    if len(body + link_block) > MAX_POST:
        # Trim summary first.
        overflow = len(body + link_block) - MAX_POST
        if len(summary) > overflow + 1:
            summary = summary[: max(0, len(summary) - overflow - 1)].rstrip() + "…"
            body = f"{head}\n\n{summary}"
        else:
            # Fall back to trimming the title.
            avail = MAX_POST - len(link_block) - len(emoji) - len(f" IL {b['identifier']} — ") - 1
            title = title[:max(0, avail)].rstrip() + "…"
            head = f"{emoji} IL {b['identifier']} — {title}"
            body = f"{head}\n\n{summary[:120]}"

    text = body + link_block

    embed_title = f"IL {b['identifier']}: {b['title']}"[:300]
    embed_desc = b["abstract"][:280] if b["abstract"] else summary
    return text, link, embed_title, embed_desc


# ---------------------------------------------------------------------------
# State
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

    print(f"Found {len(candidates)} new transportation-related bill update(s).")
    if not candidates:
        return 0

    # Newest action first.
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
        print(f"\n--- {b['identifier']} ({b['action_date']}) ---")
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
