"""Data fetchers for all four sources. Each is independently fault-tolerant."""
from __future__ import annotations

import logging
import re
import time
from typing import Any
from xml.etree import ElementTree as ET

import requests
from bs4 import BeautifulSoup

log = logging.getLogger(__name__)

UA = "Mozilla/5.0 (whale-trade-alerts; +https://github.com) Python-requests"
HEADERS = {"User-Agent": UA, "Accept": "application/json,text/html;q=0.9,*/*;q=0.8"}
EDGAR_HEADERS = {"User-Agent": "whale-trade-alerts contact@example.com",
                 "Accept-Encoding": "gzip, deflate"}
TIMEOUT = 20


def _get_with_retry(url: str, headers: dict[str, str] | None = None,
                    retries: int = 2, backoff: float = 5.0) -> requests.Response | None:
    last_exc: Exception | None = None
    for attempt in range(retries + 1):
        try:
            r = requests.get(url, headers=headers or HEADERS, timeout=TIMEOUT)
            if r.status_code == 200:
                return r
            log.warning("GET %s returned %d (attempt %d)", url, r.status_code, attempt + 1)
        except Exception as exc:
            last_exc = exc
            log.warning("GET %s raised %s (attempt %d)", url, exc, attempt + 1)
        if attempt < retries:
            time.sleep(backoff)
    if last_exc:
        log.warning("Final failure for %s: %s", url, last_exc)
    return None


# ──────────────────────────────────────────────────────────────────────────────
# Source 1 — House PTR
#
# As of mid-2026 the original house-stock-watcher.com domain and its S3 bucket
# are offline. A community fork (TattooedHead/house-stock-watcher-data) keeps
# the same JSON shape and is updated daily. We try it first, then fall back
# to the original S3 URL (which still 403s but may come back), and finally
# the dead community site, which is kept here only as a tombstone.
# ──────────────────────────────────────────────────────────────────────────────
HOUSE_SOURCES = [
    "https://raw.githubusercontent.com/TattooedHead/house-stock-watcher-data/"
    "main/data/all_transactions.json",
    "https://house-stock-watcher-data.s3-us-west-2.amazonaws.com/"
    "data/all_transactions.json",
    "https://housestockwatcher.com/api/transactions",
]


def fetch_house() -> list[dict[str, Any]]:
    for i, url in enumerate(HOUSE_SOURCES):
        try:
            r = _get_with_retry(url)
            if r is None or r.status_code != 200:
                continue
            data = r.json()
            if not isinstance(data, list) or not data:
                continue
            log.info("House PTR: %d transactions fetched (source #%d)", len(data), i + 1)
            return data
        except Exception as exc:
            log.warning("House source #%d (%s) failed: %s", i + 1, url[:60], exc)
    log.warning("House PTR: all endpoints failed")
    return []


# ──────────────────────────────────────────────────────────────────────────────
# Source 2 — Senate PTR
#
# The official senatestockwatcher.com S3 bucket is also offline. The
# timothycarambat GitHub mirror still serves the original aggregate JSON
# but stopped being updated in March 2021 — it will return historical data
# only. The dedup layer treats those records as already-seen, so they don't
# produce false alerts. Listed last is the original S3 URL as a tombstone.
# ──────────────────────────────────────────────────────────────────────────────
SENATE_SOURCES = [
    "https://raw.githubusercontent.com/timothycarambat/senate-stock-watcher-data/"
    "master/aggregate/all_transactions.json",
    "https://senate-stock-watcher-data.s3-us-west-2.amazonaws.com/"
    "aggregate/all_transactions.json",
    "https://senatestockwatcher.com/api/transactions",
]


def fetch_senate() -> list[dict[str, Any]]:
    for i, url in enumerate(SENATE_SOURCES):
        try:
            r = _get_with_retry(url)
            if r is None or r.status_code != 200:
                continue
            data = r.json()
            flattened: list[dict[str, Any]] = []
            # Some mirrors group transactions under senator objects; flatten if so.
            if (isinstance(data, list) and data and isinstance(data[0], dict)
                    and "transactions" in data[0]):
                for senator in data:
                    first = senator.get("first_name", "")
                    last = senator.get("last_name", "")
                    office = senator.get("office", "")
                    received = senator.get("date_recieved") or senator.get("date_received", "")
                    for tx in senator.get("transactions", []) or []:
                        row = dict(tx)
                        row["_senator_first"] = first
                        row["_senator_last"] = last
                        row["_office"] = office
                        row.setdefault("disclosure_date", received)
                        flattened.append(row)
            elif isinstance(data, list):
                flattened = data
            if not flattened:
                continue
            log.info("Senate PTR: %d transactions fetched (source #%d)",
                     len(flattened), i + 1)
            return flattened
        except Exception as exc:
            log.warning("Senate source #%d (%s) failed: %s", i + 1, url[:60], exc)
    log.warning("Senate PTR: all endpoints failed")
    return []


# ──────────────────────────────────────────────────────────────────────────────
# Source 3 — OGE 278-T (Executive)
# ──────────────────────────────────────────────────────────────────────────────
OGE_INDEX = "https://extapps2.oge.gov/201/Presiden.nsf/PAS+Index?OpenView"


def fetch_oge() -> list[dict[str, Any]]:
    try:
        r = _get_with_retry(OGE_INDEX, headers={"User-Agent": UA})
        if r is None:
            log.warning("OGE 278-T: index unavailable")
            return []
        soup = BeautifulSoup(r.text, "lxml")
        rows: list[dict[str, Any]] = []
        # Look for any links that resemble periodic transaction reports
        for a in soup.find_all("a"):
            text = (a.get_text() or "").strip()
            href = a.get("href", "")
            if not href:
                continue
            if re.search(r"278|periodic|transaction", text, re.I) or "278" in href.lower():
                row = {
                    "_filer": text or "Executive Branch Filer",
                    "_link": (href if href.startswith("http")
                              else "https://extapps2.oge.gov" + href),
                    "_raw_text": text,
                }
                rows.append(row)
        log.info("OGE 278-T: %d candidate filings discovered", len(rows))
        return rows
    except Exception as exc:
        log.warning("OGE 278-T unavailable: %s", exc)
        return []


# ──────────────────────────────────────────────────────────────────────────────
# Source 4 — Aschenbrenner / Situational Awareness 13F
# ──────────────────────────────────────────────────────────────────────────────
EDGAR_SEARCH = ("https://efts.sec.gov/LATEST/search-index?"
                "q=%22Aschenbrenner%22&forms=13F-HR")
EDGAR_COMPANY_FALLBACKS = [
    "https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany"
    "&company=situational+awareness&type=13F&dateb=&owner=include&count=10",
    "https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany"
    "&company=aschenbrenner&type=13F&dateb=&owner=include&count=10",
]


def fetch_aschenbrenner_13f() -> dict[str, Any]:
    """Returns {filing_date, filing_url, holdings: [{cusip, name, shares, value, put_call}], found: bool}."""
    out: dict[str, Any] = {"found": False, "holdings": [],
                           "filing_date": None, "filing_url": None}
    try:
        # EDGAR full-text search
        accession_url: str | None = None
        r = _get_with_retry(EDGAR_SEARCH, headers=EDGAR_HEADERS)
        if r is not None and r.status_code == 200:
            try:
                hits = r.json().get("hits", {}).get("hits", [])
                if hits:
                    first = hits[0].get("_source", {})
                    adsh = (first.get("adsh") or "").replace("-", "")
                    cik = first.get("ciks", [None])[0]
                    if adsh and cik:
                        accession_url = (f"https://www.sec.gov/Archives/edgar/data/"
                                         f"{int(cik)}/{adsh}/")
                        out["filing_date"] = first.get("file_date")
                        out["filing_url"] = accession_url
            except Exception as exc:
                log.warning("EDGAR search parse failed: %s", exc)

        if accession_url is None:
            log.info("No 13F found for Aschenbrenner — fund may be below $100M AUM "
                     "threshold or filing under a different name.")
            return out

        # Locate the infoTable XML in the filing index
        idx_r = _get_with_retry(accession_url + "index.json", headers=EDGAR_HEADERS)
        info_xml_url: str | None = None
        if idx_r is not None and idx_r.status_code == 200:
            try:
                items = idx_r.json().get("directory", {}).get("item", [])
                for it in items:
                    name = it.get("name", "")
                    if "infotable" in name.lower() or name.lower().endswith(".xml"):
                        info_xml_url = accession_url + name
                        if "infotable" in name.lower():
                            break
            except Exception as exc:
                log.warning("EDGAR index parse failed: %s", exc)

        if not info_xml_url:
            log.warning("Could not locate 13F infoTable XML at %s", accession_url)
            return out

        xml_r = _get_with_retry(info_xml_url, headers=EDGAR_HEADERS)
        if xml_r is None:
            return out
        out["found"] = True
        try:
            # Strip namespace for simpler parsing
            xml_text = re.sub(r' xmlns="[^"]+"', "", xml_r.text, count=1)
            root = ET.fromstring(xml_text)
            for it in root.findall(".//infoTable"):
                name = (it.findtext("nameOfIssuer") or "").strip()
                cusip = (it.findtext("cusip") or "").strip()
                value = int((it.findtext("value") or "0").replace(",", "") or 0)
                shares = int((it.findtext(".//sshPrnamt") or "0").replace(",", "") or 0)
                put_call = (it.findtext(".//putCall") or "").strip() or None
                out["holdings"].append({
                    "name": name, "cusip": cusip,
                    "shares": shares, "value": value, "put_call": put_call,
                })
        except Exception as exc:
            log.warning("13F XML parse failed: %s", exc)
        log.info("13F: %d holdings parsed", len(out["holdings"]))
        return out
    except Exception as exc:
        log.warning("Aschenbrenner 13F fetch failed: %s", exc)
        return out


def cusip_to_ticker(cusip: str) -> str:
    """Best-effort CUSIP→ticker via yfinance. Returns 'N/A' on miss."""
    if not cusip:
        return "N/A"
    try:
        import yfinance as yf  # lazy import; heavy
        info = yf.Ticker(cusip).info
        sym = info.get("symbol") if isinstance(info, dict) else None
        return (sym or "N/A").upper()
    except Exception:
        return "N/A"
