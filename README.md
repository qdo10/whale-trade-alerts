# Whale Trade Alerts 🐋

A GitHub Pages dashboard plus daily email agent that monitors congressional
stock trades, presidential / executive-branch disclosures, and Leopold
Aschenbrenner's fund (SEC 13F filings). Detects new trades since the last
run and sends a daily HTML digest at 3:00 PM PT via GitHub Actions.

## Live Dashboard

Once GitHub Pages is enabled, the site is served at:

```
https://<your-github-username>.github.io/whale-trade-alerts
```

The dashboard reads `docs/trades.json`, which is regenerated on every
agent run and committed back to the repo.

## Architecture

- `agent.py` — orchestrator (fetch → normalize → dedup → write → email)
- `fetcher.py` — per-source HTTP fetchers (House, Senate, OGE, EDGAR 13F)
- `parser.py` — normalization, options parsing, party lookup, dedup, filter
- `emailer.py` — HTML + plain-text email builder, Gmail SMTP sender
- `state.py` — `seen_trades.json` I/O and GitHub Contents API commits
- `docs/index.html` — self-contained dashboard (pure HTML/CSS/JS, no build)
- `docs/trades.json` — shared state read by the dashboard
- `seen_trades.json` — dedup memory committed back to the repo
- `.github/workflows/daily.yml` — daily scheduled GitHub Actions run

## Setup

### 1. Clone and install

```bash
git clone https://github.com/<you>/whale-trade-alerts
cd whale-trade-alerts
pip install -r requirements.txt
```

### 2. Environment variables

Create a `.env` file for local testing:

```
GMAIL_USER=you@gmail.com
GMAIL_APP_PASSWORD=xxxx-xxxx-xxxx-xxxx
ALERT_EMAIL=you@gmail.com,partner@example.com  # comma-separated for multiple
GITHUB_TOKEN=ghp_...
GITHUB_REPO=yourusername/whale-trade-alerts
```

### 3. Gmail App Password setup

Go to **myaccount.google.com → Security → 2-Step Verification → App Passwords**.
Generate one for "Mail". Use the 16-character code as `GMAIL_APP_PASSWORD`.

### 4. Enable GitHub Pages

Repo Settings → Pages → **Source: "Deploy from a branch"** → Branch: `main`
→ Folder: `/docs`. The dashboard will be live within ~1 minute.

### 5. Set GitHub Secrets

Repo Settings → **Secrets and variables → Actions → New repository secret**.
Add:

- `GMAIL_USER`
- `GMAIL_APP_PASSWORD`
- `ALERT_EMAIL`
- `FMP_API_KEY` *(optional, recommended for live Senate data —
  free at [financialmodelingprep.com](https://site.financialmodelingprep.com/developer/docs))*

`GITHUB_TOKEN` and `GITHUB_REPO` are auto-provided by Actions — do **not**
add them manually (GitHub reserves the `GITHUB_` prefix for secrets anyway).
For local runs, set `GITHUB_REPO=yourusername/whale-trade-alerts` in your
`.env` so the agent can commit `seen_trades.json` back via the API.

### Senate data: live vs. historical

Without `FMP_API_KEY`, Senate data falls through to a community mirror
that stopped updating in March 2021 — useful for historical context but
zero new alerts. With `FMP_API_KEY` set, the agent pulls live Senate
trades from Financial Modeling Prep (free tier covers our 1 call/day
needs many times over).

### 6. Test locally

```bash
python agent.py --dry-run    # prints email HTML to stdout + writes email_preview.html
python agent.py --force      # treats every trade as new and sends a real email
python agent.py              # normal run
```

### 7. Trigger manually

GitHub → **Actions** tab → "Whale Trade Alerts — Daily Run" → **Run workflow**.

## Data Sources

- **House PTRs** — STOCK Act periodic transaction reports from House members
  via [House Stock Watcher](https://housestockwatcher.com) (up to 45 days lag)
- **Senate PTRs** — STOCK Act reports from Senate members via
  [Senate Stock Watcher](https://senatestockwatcher.com) (up to 45 days lag)
- **Executive** — OGE Form 278-T filings (President, VP, Cabinet — sparse)
- **13F** — SEC EDGAR quarterly holdings, searched for "Aschenbrenner" /
  "Situational Awareness" (45 days after quarter end)

## How "new trade" detection works

Every normalized trade has an `id = sha256(person + ticker + type + date)[:16]`.
On each run, the agent compares the live ids against `seen_trades.json`.
Any id not in the file is flagged `is_new = true` and surfaced in the email
and dashboard with an amber accent. After processing, the agent updates
`seen_trades.json` and commits it back via the GitHub Contents API with
`[skip ci]` so the commit does not re-trigger the workflow.

## Email schedule

- New trades exist → email always sent
- No new trades and today is Monday → weekly summary sent anyway
- No new trades on any other day → email skipped, agent exits 0

## Disclaimer

Not financial advice. All data is sourced from public government filings
and third-party trackers; data may be incomplete or lagged. Cross-check
official sources before acting on anything you see here.
