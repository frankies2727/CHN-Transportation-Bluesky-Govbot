#!/usr/bin/env python3
"""
Weekly digest thread: a single root post + up to 6 reply posts highlighting
the most significant US transportation-bill activity from the past 7 days.

Reuses helpers from post_to_bluesky.py — no duplication of the bill loader,
filter, link builder, summarizer, or Bluesky client. The only digest-specific
logic here is the significance scorer, the per-state cap, and the thread
chaining via record.reply.{root,parent}.
"""

from __future__ import annotations

import os
import re
import sys
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

from post_to_bluesky import (
    BlueskyClient,
    JSONL_PATH,
    MAX_POST,
    BSKY_HANDLE,
    BSKY_PASSWORD,
    compose_post,
    extract_fields,
    fetch_og_image,
    is_transportation,
    load_bills,
    prepare_image_for_bluesky,
    summarize,
)

ROOT = Path(__file__).resolve().parent.parent

DIGEST_LOOKBACK_DAYS = int(os.environ.get("DIGEST_LOOKBACK_DAYS", "7"))
DIGEST_MAX_HIGHLIGHTS = int(os.environ.get("DIGEST_MAX_HIGHLIGHTS", "6"))
DIGEST_PER_STATE_CAP = int(os.environ.get("DIGEST_PER_STATE_CAP", "2"))
DRY_RUN = os.environ.get("DRY_RUN") == "1"


# ---------------------------------------------------------------------------
# Significance scoring
# ---------------------------------------------------------------------------

# Each tuple: (compiled regex, score). Highest matching tier wins.
# Govbot's action descriptions vary in capitalization and connective words
# ("Signed By The Governor" vs "signed by governor"), so the patterns below
# are deliberately tolerant of articles and case.
_SCORE_TIERS: list[tuple[re.Pattern, int]] = [
    # Became law / on governor's desk / now in force
    (re.compile(
        r"\b(?:signed|approved|delivered|sent|presented|transmitted)"
        r"\s+(?:(?:by|to)\s+)?(?:the\s+)?governor\b"
        r"|\b(?:became law|chaptered|public act|enrolled and signed|act took effect)\b"
        r"|\beffective(?:\s+immediately|\s+on\b|\s+\d|\s*$|\s*\.)",
        re.IGNORECASE), 100),
    # Passed a chamber
    (re.compile(
        r"\bpassed(?:\s+(?:by|the))?\s+(?:senate|house|assembly|chamber|both chambers)\b"
        r"|\bthird reading\s*[-,]?\s*passed\b"
        r"|\bpassed\s*[-,]?\s*third reading\b"
        r"|\bread third and passed\b"
        r"|\bresolution adopted\b|\bconference report adopted\b",
        re.IGNORECASE), 70),
    # Standalone "Passed" (often a chamber pass) -- one tier lower since terser
    (re.compile(r"^\s*passed\.?\s*$", re.IGNORECASE), 70),
    # News-worthy negative outcomes
    (re.compile(
        r"\bvetoed\b|\bveto override\b|\bdied in committee\b"
        r"|\bwithdrawn from further consideration\b|\bindefinitely postponed\b"
        r"|\blaid on the table\b",
        re.IGNORECASE), 60),
    # Committee progress
    (re.compile(
        r"\b(?:engrossed|ordered to third reading|placed on third reading"
        r"|reported favorably|reported out of committee|do pass"
        r"|reported with amendment)\b"
        r"|\bcommittee report,?\s+approving\b",
        re.IGNORECASE), 40),
    # Filed / introduced
    (re.compile(
        r"\b(?:introduced|first reading|filed|prefiled|pre-filed|read first time)\b",
        re.IGNORECASE), 20),
]


def score_action(action_desc: str) -> int:
    desc = action_desc or ""
    best = 5  # default for procedural / unmatched actions
    for pat, score in _SCORE_TIERS:
        if pat.search(desc):
            best = max(best, score)
    return best


# ---------------------------------------------------------------------------
# Selection
# ---------------------------------------------------------------------------

def in_lookback_window(action_date: str, today: datetime) -> bool:
    if not action_date:
        return False
    try:
        d = datetime.strptime(action_date, "%Y-%m-%d")
    except ValueError:
        return False
    cutoff = today - timedelta(days=DIGEST_LOOKBACK_DAYS)
    return d >= cutoff and d <= today


def select_highlights(candidates: list[dict]) -> list[dict]:
    """
    Pick the top DIGEST_MAX_HIGHLIGHTS bills, capped at DIGEST_PER_STATE_CAP
    per state, sorted by (score desc, action_date desc).
    Collapses multiple actions for the same bill to the highest-scoring one.
    """
    # Collapse to one entry per bill (state|identifier), keep highest score.
    best_by_bill: dict[tuple[str, str], dict] = {}
    for b in candidates:
        key = (b["state"], b["identifier"])
        b["_score"] = score_action(b["action_desc"])
        existing = best_by_bill.get(key)
        if existing is None or b["_score"] > existing["_score"] or (
            b["_score"] == existing["_score"] and b["action_date"] > existing["action_date"]
        ):
            best_by_bill[key] = b
    bills = list(best_by_bill.values())

    bills.sort(key=lambda b: (b["_score"], b["action_date"]), reverse=True)

    picked: list[dict] = []
    per_state: Counter[str] = Counter()
    for b in bills:
        state = b["state"] or "?"
        if per_state[state] >= DIGEST_PER_STATE_CAP:
            continue
        picked.append(b)
        per_state[state] += 1
        if len(picked) >= DIGEST_MAX_HIGHLIGHTS:
            break
    return picked


# ---------------------------------------------------------------------------
# Composition
# ---------------------------------------------------------------------------

def _format_short(d: datetime) -> str:
    abbrev = {1:"Jan", 2:"Feb", 3:"Mar", 4:"Apr", 5:"May", 6:"Jun",
              7:"Jul", 8:"Aug", 9:"Sep", 10:"Oct", 11:"Nov", 12:"Dec"}
    return f"{abbrev[d.month]} {d.day}"


def compose_root(today: datetime, total_updates: int, distinct_states: int) -> str:
    end = today
    start = today - timedelta(days=DIGEST_LOOKBACK_DAYS - 1)
    range_str = f"{_format_short(start)}–{_format_short(end)}, {end.year}"
    text = (
        "🗳️ Transportation Bills Weekly Digest\n"
        f"{range_str}\n\n"
        f"{total_updates} bill updates tracked across {distinct_states} state(s) "
        "this week. Top highlights 🧵"
    )
    if len(text) > MAX_POST:
        text = text[:MAX_POST - 1] + "…"
    return text


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

    today = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=None)

    # Filter to transportation bills with action dates in the lookback window.
    candidates: list[dict] = []
    for r in records:
        b = extract_fields(r)
        if not b:
            continue
        if not is_transportation(b):
            continue
        if not in_lookback_window(b["action_date"], today):
            continue
        candidates.append(b)

    print(f"Found {len(candidates)} transportation bill update(s) in the past "
          f"{DIGEST_LOOKBACK_DAYS} days.")
    if not candidates:
        print("No activity to digest. Exiting without posting.")
        return 0

    distinct_states = len({b["state"] or "?" for b in candidates})
    state_counts = Counter(b["state"] or "?" for b in candidates)
    print(f"  by state: {', '.join(f'{s}={n}' for s,n in state_counts.most_common(15))}")

    highlights = select_highlights(candidates)
    print(f"\nSelected {len(highlights)} highlight(s) (cap={DIGEST_MAX_HIGHLIGHTS}, "
          f"per-state-cap={DIGEST_PER_STATE_CAP}):")
    for b in highlights:
        print(f"  [{b['_score']:>3}] {b['state']} {b['identifier']} "
              f"({b['action_date']}): {b['action_desc'][:70]}")

    root_text = compose_root(today, len(candidates), distinct_states)
    print(f"\n--- ROOT ({len(root_text)} chars) ---\n{root_text}\n---")

    client = None if DRY_RUN else BlueskyClient(BSKY_HANDLE, BSKY_PASSWORD)
    root_ref: dict | None = None
    parent_ref: dict | None = None

    if client:
        result = client.post(root_text, link_url="", embed_title="", embed_desc="")
        root_ref = {"uri": result["uri"], "cid": result["cid"]}
        parent_ref = root_ref
        print(f"  posted root: {result['uri']}")
        time.sleep(2)
    else:
        root_ref = {"uri": "[dry-run-root-uri]", "cid": "[dry-run-root-cid]"}
        parent_ref = root_ref

    for b in highlights:
        summary = summarize(b)
        text, link, ec_title, ec_desc = compose_post(b, summary)

        thumb_blob = None
        if link:
            print(f"  IMG: fetching og:image for {link}")
            fetched = fetch_og_image(link)
            if fetched:
                img_bytes_raw, mime_raw = fetched
                prepared = prepare_image_for_bluesky(img_bytes_raw, mime_raw)
                if prepared and client:
                    img_bytes, img_mime = prepared
                    thumb_blob = client.upload_blob(img_bytes, img_mime)

        print(f"\n--- REPLY: {b['state']} {b['identifier']} ({b['action_date']}, "
              f"score={b['_score']}) ---\n{text}\n---")

        if client:
            try:
                reply = {"root": root_ref, "parent": parent_ref}
                result = client.post(text, link, ec_title, ec_desc,
                                     thumb_blob=thumb_blob, reply=reply)
                parent_ref = {"uri": result["uri"], "cid": result["cid"]}
                time.sleep(2)
            except Exception as e:
                print(f"  ! reply post failed: {e}", file=sys.stderr)
                continue

    print(f"\nDone. Posted thread with {len(highlights)} highlight(s).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
