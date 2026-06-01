"""Whale Trade Alerts — orchestrator."""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import traceback
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

try:
    from dotenv import load_dotenv  # type: ignore
    load_dotenv()
except Exception:
    pass

import emailer
import fetcher
import parser as p
import state

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.FileHandler("agent.log"), logging.StreamHandler()],
)
log = logging.getLogger("agent")

PAGES_URL = os.environ.get(
    "PAGES_URL",
    f"https://{(os.environ.get('GITHUB_REPO','') or '/whale-trade-alerts').split('/')[0]}"
    ".github.io/whale-trade-alerts",
)


def _build_payload(trades: list[dict], asch_meta: dict, holdings: list[dict]) -> dict:
    new_count = sum(1 for t in trades if t.get("is_new"))
    today = date.today()
    return {
        "metadata": {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "total_trades": len(trades),
            "new_trades_count": new_count,
            "date_range": f"{(today - timedelta(days=365)).isoformat()} to {today.isoformat()}",
        },
        "trades": trades,
        "aschenbrenner": {
            "last_filing_date": asch_meta.get("last_filing_date"),
            "last_filing_url": asch_meta.get("last_filing_url"),
            "positions_count": asch_meta.get("positions_count", 0),
            "top_tickers": asch_meta.get("top_tickers", []),
            "has_new_filing": asch_meta.get("has_new_filing", False),
            "holdings": holdings,
        },
    }


def run(dry_run: bool, force: bool) -> int:
    # 1. Load state
    st = state.load_state()
    seen_ids: dict[str, str] = st.get("trade_ids", {}) or {}
    prev_holdings: dict[str, dict] = st.get("aschenbrenner_previous_holdings", {}) or {}
    prev_asch_date: str | None = st.get("aschenbrenner_last_filing_date")

    # 2. Fetch all sources — each independent
    sources_ok = 0
    house_raw = fetcher.fetch_house()
    if house_raw:
        sources_ok += 1
    senate_raw = fetcher.fetch_senate()
    if senate_raw:
        sources_ok += 1
    oge_raw = fetcher.fetch_oge()
    if oge_raw:
        sources_ok += 1
    asch_raw = fetcher.fetch_aschenbrenner_13f()
    if asch_raw.get("found"):
        sources_ok += 1

    if sources_ok == 0:
        log.error("All sources failed.")
        if not dry_run:
            emailer.send_failure_alert("All four data sources failed to return data.")
        return 1

    # 3. Normalize + dedup
    house_trades = p.normalize_house(house_raw)
    senate_trades = p.normalize_senate(senate_raw)
    oge_trades = p.normalize_oge(oge_raw)
    asch_trades, current_holdings = p.normalize_13f(asch_raw, prev_holdings)

    log.info("Normalized counts → House=%d Senate=%d OGE=%d 13F-delta=%d",
             len(house_trades), len(senate_trades), len(oge_trades), len(asch_trades))

    all_trades = p.combine_and_dedup(house_trades, senate_trades, oge_trades, asch_trades)

    # 4. Filter to last 12 months
    all_trades = p.filter_last_12_months(all_trades)

    # 5. Mark new
    new_count = p.mark_new(all_trades, seen_ids, force=force)
    log.info("New trades detected: %d (of %d total)", new_count, len(all_trades))

    # Sort: is_new DESC, then amount_sort DESC, then disclosure date DESC
    all_trades.sort(key=lambda t: (not t.get("is_new"),
                                    -int(t.get("amount_sort") or 0),
                                    t.get("disclosure_date") or ""))

    # 6. Build trades.json payload
    has_new_filing = bool(asch_raw.get("found") and asch_raw.get("filing_date")
                          and asch_raw.get("filing_date") != prev_asch_date)

    # Build holdings array for dashboard
    holdings_view: list[dict] = []
    for cusip, h in current_holdings.items():
        prev = prev_holdings.get(cusip)
        if prev is None:
            change = "New"
        else:
            prev_sh = int(prev.get("shares") or 0)
            cur_sh = int(h.get("shares") or 0)
            if prev_sh == 0:
                change = "New"
            else:
                pct = (cur_sh - prev_sh) / prev_sh * 100.0
                change = f"{pct:+.0f}%"
        holdings_view.append({
            "ticker": "N/A",
            "company": h.get("name") or "",
            "shares": h.get("shares") or 0,
            "value_thousands": h.get("value") or 0,
            "change": change,
            "filed_date": asch_raw.get("filing_date") or "",
        })

    top_tickers: list[str] = []
    for h in sorted(holdings_view, key=lambda x: x["value_thousands"], reverse=True)[:6]:
        nm = (h["company"] or "").split()[0:2]
        top_tickers.append(" ".join(nm) or "—")

    asch_meta = {
        "last_filing_date": asch_raw.get("filing_date") or prev_asch_date,
        "last_filing_url": asch_raw.get("filing_url"),
        "positions_count": len(holdings_view) or len(prev_holdings),
        "top_tickers": top_tickers,
        "has_new_filing": has_new_filing,
    }

    payload = _build_payload(all_trades, asch_meta, holdings_view)

    # Always write trades.json locally so the dashboard can serve fresh data
    state.save_trades_json_local(payload)

    # 7. Decide whether to send email
    do_send = emailer.should_send_email(new_count)
    subject, html = emailer.build_email_html(all_trades, asch_meta, pages_url=PAGES_URL)
    text = emailer.build_email_text(all_trades, asch_meta)

    if dry_run:
        print("=" * 70)
        print("DRY RUN — would send:", subject)
        print("=" * 70)
        Path("email_preview.html").write_text(html, encoding="utf-8")
        log.info("Wrote email_preview.html for visual checking")
        print(html[:1200] + ("\n... [truncated]" if len(html) > 1200 else ""))
        return 0

    if not do_send:
        log.info("No new trades. Email skipped.")
    else:
        emailer.send_email(subject, html, text)

    # 9. Update state & commit
    today_iso = date.today().isoformat()
    for t in all_trades:
        seen_ids[t["id"]] = t.get("disclosure_date") or today_iso
    st["trade_ids"] = seen_ids
    if current_holdings:
        st["aschenbrenner_previous_holdings"] = current_holdings
    if asch_raw.get("filing_date"):
        st["aschenbrenner_last_filing_date"] = asch_raw["filing_date"]

    state.save_state_local(st)
    state.commit_state_files(st, payload)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Whale Trade Alerts agent")
    ap.add_argument("--dry-run", action="store_true",
                    help="Print email HTML to stdout instead of sending; no state update")
    ap.add_argument("--force", action="store_true",
                    help="Treat all trades as new (for testing)")
    args = ap.parse_args()

    try:
        return run(dry_run=args.dry_run, force=args.force)
    except Exception:
        tb = traceback.format_exc()
        log.error("Unhandled exception:\n%s", tb)
        try:
            if not args.dry_run:
                emailer.send_failure_alert(tb)
        except Exception:
            pass
        return 1


if __name__ == "__main__":
    sys.exit(main())
