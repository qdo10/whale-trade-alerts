"""Normalize raw source rows into the canonical Trade schema + dedup."""
from __future__ import annotations

import hashlib
import logging
import re
from datetime import date, datetime, timedelta
from typing import Any, TypedDict

log = logging.getLogger(__name__)


class Trade(TypedDict, total=False):
    id: str
    is_new: bool
    source: str
    person: str
    role: str
    party: str
    party_short: str
    state: str
    chamber: str
    ticker: str
    asset_description: str
    asset_type: str
    transaction_type: str
    amount: str
    amount_sort: int
    transaction_date: str
    disclosure_date: str
    filing_url: str
    option_type: str | None
    strike_price: str | None
    expiration_date: str | None
    contracts: str | None
    quantity: str | None
    thirteen_f_note: str | None


AMOUNT_RANK: dict[str, int] = {
    "$1,001–$15,000": 1,
    "$15,001–$50,000": 2,
    "$50,001–$100,000": 3,
    "$100,001–$250,000": 4,
    "$250,001–$500,000": 5,
    "$500,001–$1,000,000": 6,
    "$1,000,001–$5,000,000": 7,
    "$5,000,001–$25,000,000": 8,
    "$25,000,001+": 9,
    "~$1M–$10M (13F)": 4,
    "~$10M–$50M (13F)": 6,
    "~$50M–$100M (13F)": 7,
    "~$100M+ (13F)": 9,
}

# (last, first) → (party, short)
PARTY_LOOKUP: dict[str, tuple[str, str]] = {
    "pelosi": ("Democrat", "D"),
    "tuberville": ("Republican", "R"),
    "kelly": ("Democrat", "D"),
    "crenshaw": ("Republican", "R"),
    "greene": ("Republican", "R"),
    "khanna": ("Democrat", "D"),
    "gottheimer": ("Democrat", "D"),
    "ocasio-cortez": ("Democrat", "D"),
    "collins": ("Republican", "R"),
    "manchin": ("Independent", "I"),
    "sanders": ("Independent", "I"),
    "schiff": ("Democrat", "D"),
    "gaetz": ("Republican", "R"),
    "massie": ("Republican", "R"),
    "davidson": ("Republican", "R"),
    "sewell": ("Democrat", "D"),
    "norcross": ("Democrat", "D"),
    "moody": ("Republican", "R"),
    "fitzpatrick": ("Republican", "R"),
    "garbarino": ("Republican", "R"),
    "harshbarger": ("Republican", "R"),
    "hern": ("Republican", "R"),
    "issa": ("Republican", "R"),
    "kustoff": ("Republican", "R"),
    "lamalfa": ("Republican", "R"),
    "loudermilk": ("Republican", "R"),
    "mccaul": ("Republican", "R"),
    "mooney": ("Republican", "R"),
    "scott": ("Republican", "R"),
    "wenstrup": ("Republican", "R"),
    "blumenauer": ("Democrat", "D"),
    "cleaver": ("Democrat", "D"),
    "connolly": ("Democrat", "D"),
    "doggett": ("Democrat", "D"),
    "higgins": ("Democrat", "D"),
    "hoyer": ("Democrat", "D"),
    "lieu": ("Democrat", "D"),
    "lofgren": ("Democrat", "D"),
    "meeks": ("Democrat", "D"),
    "neal": ("Democrat", "D"),
    "panetta": ("Democrat", "D"),
    "phillips": ("Democrat", "D"),
    "raskin": ("Democrat", "D"),
    "ross": ("Democrat", "D"),
    "wexton": ("Democrat", "D"),
    "wyden": ("Democrat", "D"),
    "blackburn": ("Republican", "R"),
    "boozman": ("Republican", "R"),
    "capito": ("Republican", "R"),
    "cassidy": ("Republican", "R"),
    "cornyn": ("Republican", "R"),
    "cramer": ("Republican", "R"),
    "daines": ("Republican", "R"),
    "ernst": ("Republican", "R"),
    "fischer": ("Republican", "R"),
    "graham": ("Republican", "R"),
    "hagerty": ("Republican", "R"),
    "hoeven": ("Republican", "R"),
    "inhofe": ("Republican", "R"),
    "johnson": ("Republican", "R"),
    "lankford": ("Republican", "R"),
}


def _party_for(name: str) -> tuple[str, str]:
    if not name:
        return ("—", "—")
    parts = name.replace(",", " ").split()
    for p in parts:
        key = p.lower().strip(".")
        if key in PARTY_LOOKUP:
            return PARTY_LOOKUP[key]
    return ("Unknown", "?")


def _hash_id(*parts: str) -> str:
    s = "|".join((p or "").strip().lower() for p in parts)
    return hashlib.sha256(s.encode("utf-8")).hexdigest()[:16]


def _iso_date(raw: str | None) -> str:
    if not raw:
        return ""
    raw = raw.strip()
    fmts = ("%Y-%m-%d", "%m/%d/%Y", "%m-%d-%Y", "%b %d %Y", "%b %d, %Y",
            "%B %d, %Y", "%Y/%m/%d", "%d %b %Y")
    for f in fmts:
        try:
            return datetime.strptime(raw, f).date().isoformat()
        except ValueError:
            continue
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})", raw)
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    return raw


def _normalize_tx_type(raw: str) -> str:
    s = (raw or "").lower()
    if "purchase" in s or "buy" in s or "p" == s.strip():
        return "Buy"
    if "sale" in s or "sell" in s or "s" == s.strip() or "exchange" in s:
        return "Sell"
    return "Buy" if "p" in s else "Sell" if "s" in s else "Buy"


def _normalize_amount(raw: str) -> str:
    if not raw:
        return "$1,001–$15,000"
    # Replace various dash chars and remove whitespace around them
    cleaned = raw.replace("—", "–").replace("--", "–").replace(" - ", "–").replace("-", "–")
    cleaned = re.sub(r"\s*–\s*", "–", cleaned).strip()
    # Map common variants to canonical keys
    table = {
        "$1,001 - $15,000": "$1,001–$15,000",
        "$15,001 - $50,000": "$15,001–$50,000",
        "$50,001 - $100,000": "$50,001–$100,000",
        "$100,001 - $250,000": "$100,001–$250,000",
        "$250,001 - $500,000": "$250,001–$500,000",
        "$500,001 - $1,000,000": "$500,001–$1,000,000",
        "$1,000,001 - $5,000,000": "$1,000,001–$5,000,000",
        "$5,000,001 - $25,000,000": "$5,000,001–$25,000,000",
        "$25,000,001 +": "$25,000,001+",
        "$25,000,001 and over": "$25,000,001+",
    }
    norm = table.get(raw.strip(), cleaned)
    return norm if norm in AMOUNT_RANK else (cleaned if cleaned in AMOUNT_RANK
                                             else "$1,001–$15,000")


def _amount_rank(amt: str) -> int:
    return AMOUNT_RANK.get(amt, 1)


def _detect_asset_type(desc: str, src_type: str) -> str:
    d = (desc or "").lower()
    if any(k in d for k in (" call", " put", "option", "strike", "expir")):
        return "Option"
    if "etf" in d or "fund" in d:
        return "ETF"
    if "bond" in d or "treasury" in d or "note" in d or "bill" in d:
        return "Bond"
    if (src_type or "").lower() in ("stock", "equity", "common stock"):
        return "Stock"
    return "Stock"


_DATE_RE = re.compile(
    r"\b(\d{1,2}/\d{1,2}/\d{2,4}|"
    r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+\d{1,2},?\s+\d{2,4})\b",
    re.I,
)
_STRIKE_RE = re.compile(r"\$\s?([0-9]{1,5}(?:\.\d{1,2})?)")
_CONTRACTS_RE = re.compile(r"(\d{1,6})\s*(?:contracts?|opt)", re.I)


def _parse_option(desc: str, comment: str) -> dict[str, str | None]:
    blob = " ".join([desc or "", comment or ""])
    opt_type: str | None = None
    low = blob.lower()
    if " call" in low or low.startswith("call") or "calls" in low:
        opt_type = "Call"
    if " put" in low or low.startswith("put") or "puts" in low:
        opt_type = "Put"
    strike: str | None = None
    m = _STRIKE_RE.search(blob)
    if m:
        try:
            strike = f"{float(m.group(1)):.2f}"
        except ValueError:
            strike = None
    expiry: str | None = None
    m = _DATE_RE.search(blob)
    if m:
        expiry = m.group(1)
    contracts: str | None = None
    m = _CONTRACTS_RE.search(blob)
    if m:
        contracts = m.group(1)
    return {"option_type": opt_type, "strike_price": strike,
            "expiration_date": expiry, "contracts": contracts}


# ──────────────────────────────────────────────────────────────────────────────
# Per-source normalizers
# ──────────────────────────────────────────────────────────────────────────────
def normalize_house(rows: list[dict[str, Any]]) -> list[Trade]:
    out: list[Trade] = []
    for r in rows:
        try:
            person = (r.get("representative") or r.get("senator") or "").strip()
            ticker = (r.get("ticker") or "").upper().strip() or "N/A"
            tx_type = _normalize_tx_type(r.get("type") or "")
            tx_date = _iso_date(r.get("transaction_date"))
            disc_date = _iso_date(r.get("disclosure_date"))
            asset_desc = (r.get("asset_description") or "").strip()
            asset_type = _detect_asset_type(asset_desc, r.get("asset_type") or "")
            amount = _normalize_amount(r.get("amount") or "")
            party, party_short = _party_for(person)
            state = (r.get("district") or "")[:2].upper() if r.get("district") else ""
            url = r.get("ptr_link") or ""
            tid = _hash_id(person, ticker, tx_type, tx_date)
            opt = _parse_option(asset_desc, r.get("comment") or "") if asset_type == "Option" else {
                "option_type": None, "strike_price": None,
                "expiration_date": None, "contracts": None,
            }
            out.append(Trade(
                id=tid, is_new=False, source="House PTR", person=person,
                role="Representative", party=party, party_short=party_short,
                state=state, chamber="House", ticker=ticker,
                asset_description=asset_desc, asset_type=asset_type,
                transaction_type=tx_type, amount=amount, amount_sort=_amount_rank(amount),
                transaction_date=tx_date, disclosure_date=disc_date, filing_url=url,
                option_type=opt["option_type"], strike_price=opt["strike_price"],
                expiration_date=opt["expiration_date"], contracts=opt["contracts"],
                quantity=None, thirteen_f_note=None,
            ))
        except Exception as exc:
            log.debug("Skipping House row: %s", exc)
    return out


def normalize_senate(rows: list[dict[str, Any]]) -> list[Trade]:
    out: list[Trade] = []
    for r in rows:
        try:
            first = r.get("_senator_first") or r.get("first_name") or ""
            last = r.get("_senator_last") or r.get("last_name") or ""
            person = f"{first} {last}".strip() or (r.get("senator") or "").strip()
            ticker = (r.get("ticker") or "").upper().strip() or "N/A"
            tx_type = _normalize_tx_type(r.get("type") or "")
            tx_date = _iso_date(r.get("transaction_date"))
            disc_date = _iso_date(r.get("disclosure_date"))
            asset_desc = (r.get("asset_description") or "").strip()
            asset_type = _detect_asset_type(asset_desc, r.get("asset_type") or "")
            amount = _normalize_amount(r.get("amount") or "")
            party, party_short = _party_for(person)
            office = r.get("_office") or ""
            state = office[:2].upper() if office else ""
            url = r.get("ptr_link") or ""
            tid = _hash_id(person, ticker, tx_type, tx_date)
            opt = _parse_option(asset_desc, r.get("comment") or "") if asset_type == "Option" else {
                "option_type": None, "strike_price": None,
                "expiration_date": None, "contracts": None,
            }
            out.append(Trade(
                id=tid, is_new=False, source="Senate PTR", person=person,
                role="Senator", party=party, party_short=party_short,
                state=state, chamber="Senate", ticker=ticker,
                asset_description=asset_desc, asset_type=asset_type,
                transaction_type=tx_type, amount=amount, amount_sort=_amount_rank(amount),
                transaction_date=tx_date, disclosure_date=disc_date, filing_url=url,
                option_type=opt["option_type"], strike_price=opt["strike_price"],
                expiration_date=opt["expiration_date"], contracts=opt["contracts"],
                quantity=None, thirteen_f_note=None,
            ))
        except Exception as exc:
            log.debug("Skipping Senate row: %s", exc)
    return out


def normalize_oge(rows: list[dict[str, Any]]) -> list[Trade]:
    out: list[Trade] = []
    today = date.today().isoformat()
    for r in rows:
        try:
            person = r.get("_filer") or "Executive Branch Filer"
            url = r.get("_link") or ""
            tid = _hash_id(person, "OGE", "filing", url[:60])
            out.append(Trade(
                id=tid, is_new=False, source="OGE 278-T", person=person,
                role="President/Executive", party="—", party_short="—",
                state="", chamber="Executive", ticker="N/A",
                asset_description=r.get("_raw_text") or "Periodic Transaction Report",
                asset_type="Other", transaction_type="Buy",
                amount="$1,001–$15,000", amount_sort=1,
                transaction_date=today, disclosure_date=today, filing_url=url,
                option_type=None, strike_price=None,
                expiration_date=None, contracts=None,
                quantity=None, thirteen_f_note="Filing index entry",
            ))
        except Exception as exc:
            log.debug("Skipping OGE row: %s", exc)
    return out


def _value_band(value_thousands: int) -> str:
    v = value_thousands * 1000
    if v >= 100_000_000:
        return "~$100M+ (13F)"
    if v >= 50_000_000:
        return "~$50M–$100M (13F)"
    if v >= 10_000_000:
        return "~$10M–$50M (13F)"
    return "~$1M–$10M (13F)"


def normalize_13f(data: dict[str, Any],
                  previous_holdings: dict[str, dict[str, Any]]) -> tuple[list[Trade], dict[str, dict[str, Any]]]:
    """Returns (trades_delta, current_holdings_map_by_cusip)."""
    out: list[Trade] = []
    current: dict[str, dict[str, Any]] = {}
    if not data.get("found"):
        return out, current
    filing_date = data.get("filing_date") or date.today().isoformat()
    filing_url = data.get("filing_url") or ""
    for h in data.get("holdings", []):
        cusip = h.get("cusip") or ""
        if not cusip:
            continue
        current[cusip] = {
            "name": h.get("name") or "",
            "shares": h.get("shares") or 0,
            "value": h.get("value") or 0,
            "put_call": h.get("put_call"),
        }
    for cusip, cur in current.items():
        prev = previous_holdings.get(cusip)
        if prev is None:
            note = "New position"
            tx_type = "Buy"
            delta = cur["shares"]
        else:
            prev_sh = int(prev.get("shares") or 0)
            cur_sh = int(cur["shares"])
            if prev_sh == 0:
                pct = 100.0
            else:
                pct = (cur_sh - prev_sh) / prev_sh * 100.0
            if pct > 10:
                note = f"Increased +{pct:.0f}%"
                tx_type = "Buy"
            elif pct < 0:
                note = f"Decreased {pct:.0f}%"
                tx_type = "Sell"
            else:
                continue  # unchanged → not a delta
            delta = cur_sh - prev_sh
        # Sells for fully-exited positions
        amount = _value_band(cur["value"])
        ticker = "N/A"
        tid = _hash_id("Aschenbrenner Fund", cusip, tx_type, filing_date)
        out.append(Trade(
            id=tid, is_new=False, source="13F", person="Aschenbrenner Fund",
            role="Fund Manager", party="—", party_short="—",
            state="", chamber="Private Fund", ticker=ticker,
            asset_description=cur["name"],
            asset_type="Option" if cur.get("put_call") else "Stock",
            transaction_type=tx_type, amount=amount, amount_sort=_amount_rank(amount),
            transaction_date=filing_date, disclosure_date=filing_date,
            filing_url=filing_url,
            option_type=cur.get("put_call"), strike_price=None,
            expiration_date=None, contracts=None,
            quantity=f"Δ {delta:+,} shares",
            thirteen_f_note=note,
        ))
    # Detect sold-out positions
    for cusip, prev in previous_holdings.items():
        if cusip not in current:
            tid = _hash_id("Aschenbrenner Fund", cusip, "Sell", filing_date)
            out.append(Trade(
                id=tid, is_new=False, source="13F", person="Aschenbrenner Fund",
                role="Fund Manager", party="—", party_short="—",
                state="", chamber="Private Fund", ticker="N/A",
                asset_description=prev.get("name") or cusip,
                asset_type="Stock", transaction_type="Sell",
                amount="~$1M–$10M (13F)", amount_sort=4,
                transaction_date=filing_date, disclosure_date=filing_date,
                filing_url=filing_url,
                option_type=None, strike_price=None,
                expiration_date=None, contracts=None,
                quantity=f"Δ -{int(prev.get('shares') or 0):,} shares",
                thirteen_f_note="Exited position",
            ))
    return out, current


# ──────────────────────────────────────────────────────────────────────────────
# Combination, dedup, filtering
# ──────────────────────────────────────────────────────────────────────────────
def combine_and_dedup(*lists: list[Trade]) -> list[Trade]:
    seen: dict[str, Trade] = {}
    for lst in lists:
        for t in lst:
            seen.setdefault(t["id"], t)
    return list(seen.values())


def filter_last_12_months(trades: list[Trade]) -> list[Trade]:
    cutoff = (date.today() - timedelta(days=365)).isoformat()
    out: list[Trade] = []
    for t in trades:
        td = t.get("transaction_date") or ""
        if not td or td >= cutoff:
            out.append(t)
    return out


def mark_new(trades: list[Trade], seen_ids: dict[str, str], force: bool = False) -> int:
    n = 0
    for t in trades:
        if force or t["id"] not in seen_ids:
            t["is_new"] = True
            n += 1
    return n
