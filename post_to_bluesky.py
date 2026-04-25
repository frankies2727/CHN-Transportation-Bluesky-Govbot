#!/usr/bin/env python3
"""
Post new govbot RSS items to Bluesky with AI summaries.

- Scans docs/ for RSS feeds produced by `govbot build`.
- Tracks posted GUIDs in state/posted.json (committed back to repo).
- Summarizes each new item with Claude.
- Posts to Bluesky with a clickable link facet.

Env vars required:
    BLUESKY_HANDLE        e.g. mybot.bsky.social
    BLUESKY_APP_PASSWORD  app password (NOT main password)
    ANTHROPIC_API_KEY     for summaries

Optional:
    POST_LIMIT            max posts per run (default 5, prevents flooding)
    DRY_RUN               if "1", print what would be posted but don't post
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable
from xml.etree import ElementTree as ET

import requests

ROOT = Path(__file__).resolve().parent.parent
DOCS_DIR = ROOT / "docs"
STATE_FILE = ROOT / "state" / "posted.json"
POST_LIMIT = int(os.environ.get("POST_LIMIT", "5"))
DRY_RUN = os.environ.get("DRY_RUN") == "1"

BSKY_HANDLE = os.environ.get("BLUESKY_HANDLE", "")
BSKY_PASSWORD = os.environ.get("BLUESKY_APP_PASSWORD", "")
ANTHROPIC_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

BLUESKY_API = "https://bsky.social/xrpc"
ANTHROPIC_API = "https://api.anthropic.com/v1/messages"
ANTHROPIC_MODEL = "claude-haiku-4-5-20251001"  # fast + cheap for summaries


@dataclass
class FeedItem:
    guid: str
    title: str
    link: str
    description: str
    pub_date: str
    feed_name: str  # which RSS file it came from (used as a tag hint)


# ----------------------------- RSS parsing ----------------------------------

def _text(elem: ET.Element | None) -> str:
    return (elem.text or "").strip() if elem is not None else ""


def parse_feed(path: Path) -> list[FeedItem]:
    """Parse an RSS 2.0 file. Forgiving: we only require <item><title> + a stable id."""
    try:
        tree = ET.parse(path)
    except ET.ParseError as e:
        print(f"  ! skip {path.name}: {e}", file=sys.stderr)
        return []

    root = tree.getroot()
    items: list[FeedItem] = []
    feed_name = path.stem

    for item in root.iter("item"):
        title = _text(item.find("title"))
        link = _text(item.find("link"))
        desc = _text(item.find("description"))
        pub = _text(item.find("pubDate"))
        guid = _text(item.find("guid")) or link or f"{feed_name}:{title}"

        if not title:
            continue

        items.append(FeedItem(
            guid=guid,
            title=title,
            link=link,
            description=desc,
            pub_date=pub,
            feed_name=feed_name,
        ))
    return items


def collect_new_items(seen: set[str]) -> list[FeedItem]:
    if not DOCS_DIR.exists():
        print(f"No docs/ directory found at {DOCS_DIR}; did `govbot build` run?")
        return []

    new: list[FeedItem] = []
    feeds = sorted(p for p in DOCS_DIR.rglob("*") if p.suffix.lower() in {".xml", ".rss"})
    print(f"Found {len(feeds)} feed file(s) in docs/")

    for feed_path in feeds:
        for item in parse_feed(feed_path):
            if item.guid not in seen:
                new.append(item)

    # Sort newest-first by pub_date when available; falls back to feed order.
    def sort_key(it: FeedItem):
        try:
            from email.utils import parsedate_to_datetime
            return parsedate_to_datetime(it.pub_date)
        except Exception:
            return datetime.min.replace(tzinfo=timezone.utc)

    new.sort(key=sort_key, reverse=True)
    return new


# ----------------------------- Summarization --------------------------------

def strip_html(s: str) -> str:
    return re.sub(r"<[^>]+>", "", s).strip()


def summarize(item: FeedItem) -> str:
    """Ask Claude for a tight, neutral summary that fits in a Bluesky post."""
    if not ANTHROPIC_KEY:
        # Fallback: truncate the description.
        return strip_html(item.description)[:200]

    body = strip_html(item.description) or item.title
    prompt = (
        "You are summarizing a US legislative bill for a civic-engagement Bluesky bot.\n"
        "Write ONE plain-text sentence (under 180 characters) that states what the bill "
        "does, neutrally. No emoji, no hashtags, no editorializing, no quotes around the "
        "summary, no leading phrases like 'This bill'. Just the substance.\n\n"
        f"Title: {item.title}\n"
        f"Description: {body[:2000]}"
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
        text = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
        return text.strip().strip('"').strip()
    except Exception as e:
        print(f"  ! summarization failed, falling back: {e}", file=sys.stderr)
        return strip_html(item.description)[:180] or item.title


# ----------------------------- Bluesky --------------------------------------

class BlueskyClient:
    def __init__(self, handle: str, password: str):
        self.handle = handle
        self.session = requests.Session()
        r = self.session.post(
            f"{BLUESKY_API}/com.atproto.server.createSession",
            json={"identifier": handle, "password": password},
            timeout=30,
        )
        r.raise_for_status()
        data = r.json()
        self.did = data["did"]
        self.jwt = data["accessJwt"]
        self.session.headers["Authorization"] = f"Bearer {self.jwt}"

    def post(self, text: str, link_url: str | None = None) -> dict:
        """Post text. If link_url is given, the trailing URL in `text` becomes a clickable facet."""
        record = {
            "$type": "app.bsky.feed.post",
            "text": text,
            "createdAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        }

        if link_url and link_url in text:
            # Bluesky facets use UTF-8 byte offsets, not character offsets.
            text_bytes = text.encode("utf-8")
            url_bytes = link_url.encode("utf-8")
            byte_start = text_bytes.find(url_bytes)
            if byte_start >= 0:
                record["facets"] = [{
                    "index": {
                        "byteStart": byte_start,
                        "byteEnd": byte_start + len(url_bytes),
                    },
                    "features": [{
                        "$type": "app.bsky.richtext.facet#link",
                        "uri": link_url,
                    }],
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


# ----------------------------- Composition ----------------------------------

# Bluesky's hard limit is 300 graphemes; we stay safely under.
MAX_POST = 290


def compose_post(item: FeedItem, summary: str) -> tuple[str, str | None]:
    """
    Returns (text, link_url). Layout:

        <Title>
        <summary>
        <link>
    """
    # Make tag prefix from feed file name if it's clearly a tag (e.g. "housing.xml" -> "[housing]").
    tag = ""
    fname = item.feed_name.lower()
    if fname not in {"index", "feed", "all"}:
        tag = f"[{item.feed_name}] "

    title = item.title.strip()
    link = item.link.strip() or None

    # Reserve room for link + newlines.
    link_block = f"\n{link}" if link else ""
    available = MAX_POST - len(tag) - len(link_block) - 2  # 2 newlines between title/summary
    head = f"{tag}{title}"

    summary = summary.strip()
    body = f"{head}\n\n{summary}" if summary else head

    if len(body) + len(link_block) > MAX_POST:
        # Trim summary first, then title.
        overflow = (len(body) + len(link_block)) - MAX_POST
        if len(summary) > overflow + 1:
            summary = summary[: max(0, len(summary) - overflow - 1)].rstrip() + "…"
            body = f"{head}\n\n{summary}"
        else:
            body = head[: MAX_POST - len(link_block) - 1].rstrip() + "…"

    return body + link_block, link


# ----------------------------- State ----------------------------------------

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


# ----------------------------- Main -----------------------------------------

def main() -> int:
    if not DRY_RUN and (not BSKY_HANDLE or not BSKY_PASSWORD):
        print("ERROR: BLUESKY_HANDLE and BLUESKY_APP_PASSWORD must be set.", file=sys.stderr)
        return 1

    state = load_state()
    seen = set(state.get("posted", []))

    new_items = collect_new_items(seen)
    print(f"{len(new_items)} new item(s); will post up to {POST_LIMIT}.")

    if not new_items:
        return 0

    client: BlueskyClient | None = None
    if not DRY_RUN:
        client = BlueskyClient(BSKY_HANDLE, BSKY_PASSWORD)

    posted_count = 0
    for item in new_items[:POST_LIMIT]:
        summary = summarize(item)
        text, link = compose_post(item, summary)
        print(f"\n--- {item.guid} ---\n{text}\n---")

        if client:
            try:
                client.post(text, link_url=link)
                posted_count += 1
                time.sleep(2)  # be polite to the PDS
            except requests.HTTPError as e:
                print(f"  ! post failed: {e.response.status_code} {e.response.text}", file=sys.stderr)
                continue
        else:
            posted_count += 1

        seen.add(item.guid)

    # Persist state regardless of dry-run so subsequent runs reflect intent.
    state["posted"] = sorted(seen)
    state["last_run"] = datetime.now(timezone.utc).isoformat()
    save_state(state)

    print(f"\nDone. Posted {posted_count} item(s). State saved to {STATE_FILE.relative_to(ROOT)}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
