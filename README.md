# govbot-bluesky

A Bluesky bot that posts new tagged legislative bills with AI summaries, powered by [chihacknight/govbot](https://github.com/chihacknight/govbot). Runs on GitHub Actions for free.

## How it works

On a cron (every 6 hours by default):

1. GitHub Actions installs and runs `govbot`, which clones state legislation repos, tags bills against your `govbot.yml`, and emits RSS feeds into `docs/`.
2. The workflow installs [Ollama](https://ollama.com/) on the runner and pulls a small **Qwen** model (`qwen2.5:1.5b`) — summarization runs entirely on the runner, no third-party API key required.
3. `scripts/post_to_bluesky.py` walks `docs/` for new items (deduped by RSS `<guid>`), asks the local Qwen model for a one-sentence neutral summary, and posts to Bluesky with a clickable link.
4. The workflow commits `state/posted.json` back to the repo so the next run knows what's already been posted.

A second workflow runs **every Friday at ~4 pm ET** (`weekly-digest.yml`) and posts a threaded weekly digest: a root post summarizing the week's transportation-bill activity plus up to 6 reply posts highlighting the most significant updates (signed into law, passed, vetoed, etc.). Bills are scored by action significance and capped at 2 per state to keep the digest broad. Configure via env vars in the workflow: `DIGEST_LOOKBACK_DAYS`, `DIGEST_MAX_HIGHLIGHTS`, `DIGEST_PER_STATE_CAP`.

## Setup

### 1. Use this repo as a template
Click **Use this template** on GitHub (or fork). Clone locally.

### 2. Add a `govbot.yml`
Run `govbot` locally once with no config — it launches a wizard that creates `govbot.yml` for you (pick states and tags). Commit the result.

If you'd rather skip the wizard, see the [govbot docs](https://chihacknight.github.io/govbot/).

### 3. Add repository secrets
In **Settings → Secrets and variables → Actions**, add two secrets:

| Secret | Value |
| --- | --- |
| `BLUESKY_HANDLE` | Your bot's handle, e.g. `mybot.bsky.social` |
| `BLUESKY_APP_PASSWORD` | An app password from Bluesky **Settings → App Passwords** (not your main password!) |

Summarization uses a local Qwen model via Ollama on the GitHub Actions runner, so no third-party LLM API key is needed.

### 4. Enable Actions
On the Actions tab, enable workflows. The first run can be triggered manually via **Run workflow** on `govbot-bluesky`.

## Configuration knobs

Edit `.github/workflows/post.yml`:

- `cron:` — change the schedule. `0 */6 * * *` is every 6 hours.
- `POST_LIMIT` — max posts per run (default 5). Prevents flooding if many bills land at once.

Edit `scripts/post_to_bluesky.py` (or override via env vars in the workflow):

- `QWEN_MODEL` — default is `qwen2.5:1.5b` (fast on a 2-core CI runner). Bump to `qwen2.5:3b` or `qwen2.5:7b` for richer summaries — pull time and per-summary latency will go up accordingly.
- `QWEN_API_URL` — defaults to `http://localhost:11434/api/chat` (Ollama). Point at any Ollama-compatible endpoint to use a different host.
- `QWEN_TIMEOUT` — per-request timeout in seconds (default 180).
- `MAX_POST` — post length cap. Bluesky's actual limit is 300 graphemes; we keep some slack.

## Local testing

```bash
# 1. Install Ollama (https://ollama.com/) and pull the Qwen model
ollama pull qwen2.5:1.5b
# Make sure `ollama serve` is running (the desktop app starts it automatically;
# on Linux the install script enables a systemd service).

# 2. Run the script in dry-run mode
pip install -r requirements.txt
DRY_RUN=1 python scripts/post_to_bluesky.py
```

Dry run prints composed posts without hitting Bluesky. State still updates so you can iterate without re-summarizing.

If you don't have Ollama running locally, summaries fall back to a truncated abstract — the rest of the pipeline still works.

## Layout

```
.github/workflows/post.yml   # cron + pipeline
scripts/post_to_bluesky.py   # the bot
state/posted.json            # GUIDs we've already posted (committed)
docs/                        # RSS feeds (created by govbot)
govbot.yml                   # your sources & tags (you create this)
```

## Notes & gotchas

- **First run is loud.** Without a `state/posted.json`, every existing item in your feeds is "new". The `POST_LIMIT` cap protects you, but consider seeding the state file with current GUIDs (see below) before enabling the cron.
- **Idempotency** is via RSS `<guid>`. If govbot's RSS doesn't include guids, the bot falls back to the link, then to a `feed_name:title` synthetic id.
- **Permissions.** The workflow needs `contents: write` to commit state back. This is set in the workflow file already, but org-level settings can override it — check **Settings → Actions → General → Workflow permissions** if commits aren't landing.

### Seeding state to skip the backlog

```bash
# After your first govbot run produces docs/:
python -c "
import json, glob
from xml.etree import ElementTree as ET
guids = []
for f in glob.glob('docs/**/*.xml', recursive=True):
    for item in ET.parse(f).getroot().iter('item'):
        g = item.findtext('guid') or item.findtext('link') or item.findtext('title')
        if g: guids.append(g.strip())
import os; os.makedirs('state', exist_ok=True)
json.dump({'posted': sorted(set(guids))}, open('state/posted.json','w'), indent=2)
print(f'Seeded {len(set(guids))} GUIDs.')
"
git add state/posted.json && git commit -m "seed posted state" && git push
```

## License

MIT — do whatever.
