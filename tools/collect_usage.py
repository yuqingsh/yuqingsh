#!/usr/bin/env python3
"""Collect daily token usage from AI clients into data/usage.json.

Sources (all raw data stays on its host; only aggregated JSON is pushed):
  - Codex (ChatGPT), local:  ~/.codex/{sessions,archived_sessions}
  - Codex (ChatGPT), remote: same paths on hosts in CODEX_REMOTE_HOSTS,
                             scanned by streaming tools/codex_usage_scan.py
                             over ssh (nothing installed remotely)
  - Cursor:          local accessToken -> api2.cursor.sh GetFilteredUsageEvents
  - DeepSeek Harness: ~/.dsh session JSONs (cumulative per session, diffed per run)

The aggregated result is committed & pushed so a GitHub Action can render the SVG.

Usage:
  python3 tools/collect_usage.py [--no-git] [--verbose]
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from codex_usage_scan import scan_codex  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_FILE = REPO_ROOT / "data" / "usage.json"
SCANNER_FILE = REPO_ROOT / "tools" / "codex_usage_scan.py"

CODEX_BASE_DIR = Path.home() / ".codex"
CODEX_REMOTE_HOSTS = ["wuyou-dev"]
DSH_SESSIONS_DIR = Path.home() / ".dsh" / "storages" / "session_projcache" / "sessions"
CURSOR_DB = (
    Path.home()
    / "Library"
    / "Application Support"
    / "Cursor"
    / "User"
    / "globalStorage"
    / "state.vscdb"
)
CURSOR_API = "https://api2.cursor.sh/aiserver.v1.DashboardService/GetFilteredUsageEvents"

VERBOSE = "--verbose" in sys.argv


def log(msg):
    print(f"[collect] {msg}", flush=True)


def vlog(msg):
    if VERBOSE:
        log(msg)


def local_day_from_iso(ts: str) -> str:
    """UTC ISO-8601 -> local YYYY-MM-DD."""
    dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    return dt.astimezone().strftime("%Y-%m-%d")


def local_day_from_ms(ms) -> str:
    dt = datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc)
    return dt.astimezone().strftime("%Y-%m-%d")


def today_local() -> str:
    return datetime.now().astimezone().strftime("%Y-%m-%d")


# ---------------------------------------------------------------- Codex

def collect_codex_local() -> dict:
    """Scan this machine's ~/.codex (authoritative full re-scan)."""
    result = scan_codex(CODEX_BASE_DIR)
    log(f"codex(local): {result['events']} token events across {result['files']} session files")
    return result["days"]


def collect_codex_remote(host: str) -> dict | None:
    """Stream the scanner over ssh and return its per-day dict, or None on failure.

    Nothing is installed on the remote host; only compact JSON comes back.
    """
    try:
        proc = subprocess.run(
            [
                "ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
                host, "python3", "-",
            ],
            input=SCANNER_FILE.read_text(encoding="utf-8"),
            capture_output=True,
            text=True,
            timeout=900,
        )
        if proc.returncode != 0:
            log(f"codex({host}): ssh scan failed: {proc.stderr.strip()[:200]}")
            return None
        result = json.loads(proc.stdout)
        log(
            f"codex({host}): {result['events']} token events "
            f"across {result['files']} session files"
        )
        return result["days"]
    except (subprocess.TimeoutExpired, ValueError, OSError) as e:
        log(f"codex({host}): scan error ({e}), keeping previous data")
        return None


# ---------------------------------------------------------------- Cursor

def cursor_access_token() -> str | None:
    if not CURSOR_DB.is_file():
        return None
    try:
        uri = f"file:{CURSOR_DB}?mode=ro&immutable=1"
        con = sqlite3.connect(uri, uri=True, timeout=5)
        row = con.execute(
            "SELECT value FROM ItemTable WHERE key='cursorAuth/accessToken'"
        ).fetchone()
        con.close()
        return row[0] if row else None
    except sqlite3.Error as e:
        log(f"cursor: cannot read access token: {e}")
        return None


def cursor_post(token: str, body: dict) -> dict:
    req = urllib.request.Request(
        CURSOR_API,
        data=json.dumps(body).encode(),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode())


def collect_cursor() -> dict:
    """Return {day: {model: {...}}} for the window the Cursor API exposes."""
    token = cursor_access_token()
    if not token:
        log("cursor: no access token (not logged in?), skipped")
        return {}
    days = {}
    page, page_size, total = 1, 100, None
    n_events = 0
    try:
        while True:
            d = cursor_post(token, {"pageSize": page_size, "page": page})
            events = d.get("usageEventsDisplay") or []
            if total is None:
                total = d.get("totalUsageEventsCount", 0)
            if not events:
                break
            for ev in events:
                tu = ev.get("tokenUsage") or {}
                day = local_day_from_ms(ev["timestamp"])
                b = days.setdefault(day, {}).setdefault(
                    ev.get("model") or "unknown",
                    {"input": 0, "output": 0, "cache_read": 0},
                )
                b["input"] += tu.get("inputTokens", 0)
                b["output"] += tu.get("outputTokens", 0)
                b["cache_read"] += tu.get("cacheReadTokens", 0)
                n_events += 1
            if n_events >= total or len(events) < page_size:
                break
            page += 1
    except Exception as e:  # token expired / network down: keep other sources
        log(f"cursor: API error ({e}), skipped")
        return {}
    log(f"cursor: {n_events} usage events (API reports {total})")
    return days


# ------------------------------------------------------- DeepSeek Harness

def collect_dsh(prev_state: dict) -> tuple[dict, dict]:
    """Diff cumulative per-session totals.

    Returns ({day: {model: {...}}}, new_state). New sessions are backfilled to
    their last-activity day; known sessions attribute the delta to today.
    """
    days, new_state = {}, {}
    if not DSH_SESSIONS_DIR.is_dir():
        log("dsh: sessions dir not found, skipped")
        return days, new_state
    today = today_local()
    n_sessions = 0
    for path in sorted(DSH_SESSIONS_DIR.glob("*.json")):
        sid = path.stem
        try:
            d = json.loads(path.read_text(encoding="utf-8", errors="replace"))
            rows = (d.get("record") or {}).get("rows") or {}
            totals = ((rows.get("tokenUsage") or {}).get("val") or {}).get("totals") or {}
            cur = {
                "input": int(totals.get("uncachedInputTokens", 0) or 0),
                "output": int(totals.get("outputTokens", 0) or 0),
                "cache_read": int(totals.get("cacheReadTokens", 0) or 0),
            }
            model = (
                (((rows.get("modelSelection") or {}).get("val") or {}).get("lastUsed") or {})
                .get("model")
            ) or "unknown"
            last_prompt_at = ((rows.get("sessionListMetadata") or {}).get("val") or {}).get(
                "lastPromptAt"
            )
        except (ValueError, OSError) as e:
            vlog(f"dsh: cannot parse {path.name}: {e}")
            if sid in prev_state:
                new_state[sid] = prev_state[sid]
            continue
        prev = prev_state.get(sid)
        if prev is None and cur == {"input": 0, "output": 0, "cache_read": 0}:
            new_state[sid] = cur
            continue
        if prev is None:
            # first time we see this session: backfill to its last-active day
            day = local_day_from_ms(last_prompt_at) if last_prompt_at else today
            delta = cur
        else:
            day = today
            delta = {k: max(0, cur[k] - prev.get(k, 0)) for k in cur}
        if any(delta.values()):
            b = days.setdefault(day, {}).setdefault(
                model, {"input": 0, "output": 0, "cache_read": 0}
            )
            b["input"] += delta["input"]
            b["output"] += delta["output"]
            b["cache_read"] += delta["cache_read"]
            n_sessions += 1
        new_state[sid] = cur
    log(f"dsh: {n_sessions} sessions with new usage (tracked: {len(new_state)})")
    return days, new_state


# ------------------------------------------------------------------ merge

def load_data() -> dict:
    if DATA_FILE.is_file():
        try:
            return json.loads(DATA_FILE.read_text(encoding="utf-8"))
        except ValueError:
            log("warning: data/usage.json is corrupt, starting fresh")
    return {}


def main():
    if "--no-git" not in sys.argv:
        # keep the (shadow) clone in sync with bot-rendered commits
        subprocess.run(["git", "pull", "--rebase"], cwd=REPO_ROOT, check=True)

    data = load_data()
    days = data.setdefault("days", {})
    state = data.setdefault("state", {})

    # Codex: full re-scans are authoritative; overwrite each source's entries.
    # On remote failure the previous entries for that source are preserved.
    codex_sources = {"codex": collect_codex_local()}
    for host in CODEX_REMOTE_HOSTS:
        remote = collect_codex_remote(host)
        if remote is not None:
            codex_sources[f"codex-{host}"] = remote
    for source, source_days in codex_sources.items():
        for day, models in source_days.items():
            days.setdefault(day, {})[source] = {"models": models}

    # Cursor: overwrite only days inside the API window; older days persist.
    for day, models in collect_cursor().items():
        days.setdefault(day, {})["cursor"] = {"models": models}

    # DSH: deltas merged on top of stored values.
    dsh_days, state["dsh_sessions"] = collect_dsh(state.get("dsh_sessions") or {})
    for day, models in dsh_days.items():
        bucket = days.setdefault(day, {}).setdefault("dsh-kimi", {})
        for model, u in models.items():
            m = bucket.setdefault("models", {}).setdefault(
                model, {"input": 0, "output": 0, "cache_read": 0}
            )
            m["input"] += u["input"]
            m["output"] += u["output"]
            m["cache_read"] += u["cache_read"]

    def without_ts(d):
        return {k: v for k, v in d.items() if k != "updated_at"}

    old_data = {}
    if DATA_FILE.is_file():
        try:
            old_data = json.loads(DATA_FILE.read_text(encoding="utf-8"))
        except ValueError:
            pass
    data["updated_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
    data["days"] = dict(sorted(days.items()))
    if without_ts(data) == without_ts(old_data):
        log("no changes")
        return 0

    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    new_text = json.dumps(data, ensure_ascii=False, indent=1, sort_keys=False) + "\n"
    DATA_FILE.write_text(new_text, encoding="utf-8")
    log(f"wrote {DATA_FILE.relative_to(REPO_ROOT)} ({len(days)} days)")

    if "--no-git" in sys.argv:
        return 0
    subprocess.run(["git", "add", "data/usage.json"], cwd=REPO_ROOT, check=True)
    subprocess.run(
        ["git", "commit", "-m", "chore: update token usage data"],
        cwd=REPO_ROOT,
        check=True,
    )
    subprocess.run(["git", "push"], cwd=REPO_ROOT, check=True)
    log("committed and pushed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
