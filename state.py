"""Persistent state: seen_trades.json local I/O + GitHub API commits."""
from __future__ import annotations

import base64
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

log = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parent
SEEN_PATH = REPO_ROOT / "seen_trades.json"
TRADES_JSON_PATH = REPO_ROOT / "docs" / "trades.json"

EMPTY_STATE: dict[str, Any] = {
    "trade_ids": {},
    "aschenbrenner_previous_holdings": {},
    "aschenbrenner_last_filing_date": None,
    "last_run": None,
}


def load_state() -> dict[str, Any]:
    if not SEEN_PATH.exists():
        log.info("seen_trades.json missing; starting fresh")
        return json.loads(json.dumps(EMPTY_STATE))
    try:
        with SEEN_PATH.open("r", encoding="utf-8") as f:
            data = json.load(f)
        for k, v in EMPTY_STATE.items():
            data.setdefault(k, v if not isinstance(v, dict) else {})
        return data
    except Exception as exc:
        log.warning("Failed to read seen_trades.json (%s); using empty state", exc)
        return json.loads(json.dumps(EMPTY_STATE))


def save_state_local(state: dict[str, Any]) -> None:
    state["last_run"] = datetime.now(timezone.utc).isoformat()
    SEEN_PATH.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")
    log.info("Wrote seen_trades.json locally (%d trade ids)", len(state.get("trade_ids", {})))


def save_trades_json_local(payload: dict[str, Any]) -> None:
    TRADES_JSON_PATH.parent.mkdir(parents=True, exist_ok=True)
    TRADES_JSON_PATH.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    log.info("Wrote docs/trades.json locally (%d trades)", len(payload.get("trades", [])))


def _gh_headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _get_file_sha(repo: str, path: str, token: str) -> str | None:
    url = f"https://api.github.com/repos/{repo}/contents/{path}"
    r = requests.get(url, headers=_gh_headers(token), timeout=20)
    if r.status_code == 200:
        return r.json().get("sha")
    if r.status_code == 404:
        return None
    log.warning("GitHub GET %s returned %d: %s", path, r.status_code, r.text[:200])
    return None


def commit_file_via_api(repo: str, path: str, content_bytes: bytes, message: str, token: str) -> bool:
    """Create or update a file in the repo via the GitHub Contents API."""
    if not (repo and token):
        log.info("Skipping GitHub commit for %s (no repo/token)", path)
        return False
    sha = _get_file_sha(repo, path, token)
    url = f"https://api.github.com/repos/{repo}/contents/{path}"
    body: dict[str, Any] = {
        "message": message,
        "content": base64.b64encode(content_bytes).decode("ascii"),
    }
    if sha:
        body["sha"] = sha
    r = requests.put(url, headers=_gh_headers(token), json=body, timeout=20)
    if r.status_code in (200, 201):
        log.info("Committed %s via GitHub API", path)
        return True
    log.warning("Commit %s failed (%d): %s", path, r.status_code, r.text[:300])
    return False


def commit_state_files(state: dict[str, Any], trades_payload: dict[str, Any]) -> None:
    repo = os.environ.get("GITHUB_REPO", "")
    token = os.environ.get("GITHUB_TOKEN", "")
    if not (repo and token):
        log.info("GITHUB_REPO/GITHUB_TOKEN not set; skipping API commits (local files already written)")
        return
    seen_bytes = json.dumps(state, indent=2, sort_keys=True).encode("utf-8")
    trades_bytes = json.dumps(trades_payload, indent=2).encode("utf-8")
    commit_file_via_api(repo, "seen_trades.json", seen_bytes,
                        "chore: update seen trades", token)
    commit_file_via_api(repo, "docs/trades.json", trades_bytes,
                        "chore: refresh trades data", token)
