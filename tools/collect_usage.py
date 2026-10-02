#!/usr/bin/env python3
"""Collect daily token usage from local AI clients into data/usage.json.

Sources (all data stays on this machine except the aggregated JSON):
  - Codex (ChatGPT): ~/.codex/sessions/**/*.jsonl  token_count events
  - Cursor:          local accessToken -> api2.cursor.sh GetFilteredUsageEvents
  - DeepSeek Harness: ~/.dsh session JSONs (cumulative per session, diffed per run)

The aggregated result is committed & pushed so a GitHub Action can render the SVG.

Usage:
  python3 tools/collect_usage.py [--no-git] [--verbose]
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_FILE = REPO_ROOT / "data" / "usage.json"

CODEX_SESSIONS_DIRS = [
    Path.home() / ".codex" / "sessions",
    Path.home() / ".codex" / "archived_sessions",
]
CODEX_CONFIG = Path.home() / ".codex" / "config.toml"
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

def codex_model_name() -> str:
    try:
        text = CODEX_CONFIG.read_text(encoding="utf-8")
        m = re.search(r'^\s*model\s*=\s*"([^"]+)"', text, re.M)
        if m:
            return m.group(1)
    except OSError:
        pass
    return "codex"


def collect_codex() -> dict:
    """Return {day: {model: {input, output, cache_read}}} from all Codex sessions.

    Scans both ~/.codex/sessions and ~/.codex/archived_sessions (archived
    threads hold the majority of tokens). Per-day attribution sums
    last_token_usage deltas; afterwards each session is reconciled against its
    final total_token_usage (authoritative — matches the Codex threads DB) and
    any unaccounted remainder is attributed to the session's last active day.
    """
    days = {}
    files = []
    for d in CODEX_SESSIONS_DIRS:
        if d.is_dir():
            files.extend(d.rglob("*.jsonl"))
    if not files:
        log("codex: no session files found, skipped")
        return days
    model = codex_model_name()
    n_events = 0

    def bucket(day):
        return days.setdefault(day, {}).setdefault(
            model, {"input": 0, "output": 0, "cache_read": 0}
        )

    for path in sorted(files):
        inc = {"input": 0, "output": 0, "cache_read": 0}
        last_total = None
        last_day = None
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    if '"token_count"' not in line:
                        continue
                    try:
                        d = json.loads(line)
                        payload = d.get("payload") or {}
                        if payload.get("type") != "token_count":
                            continue
                        info = payload.get("info") or {}
                        usage = info.get("last_token_usage") or {}
                    except (ValueError, AttributeError):
                        continue
                    day = local_day_from_iso(d["timestamp"])
                    last_day = day
                    # OpenAI semantics: input_tokens INCLUDES cached_input_tokens
                    cached = usage.get("cached_input_tokens", 0)
                    fresh = max(0, usage.get("input_tokens", 0) - cached)
                    out = usage.get("output_tokens", 0)
                    inc["input"] += fresh
                    inc["output"] += out
                    inc["cache_read"] += cached
                    b = bucket(day)
                    b["input"] += fresh
                    b["output"] += out
                    b["cache_read"] += cached
                    if info.get("total_token_usage"):
                        last_total = info["total_token_usage"]
                    n_events += 1
        except OSError as e:
            vlog(f"codex: cannot read {path}: {e}")
            continue
        # reconcile against the session's authoritative final totals
        if last_total and last_day:
            tot_cached = last_total.get("cached_input_tokens", 0)
            tot_fresh = max(0, last_total.get("input_tokens", 0) - tot_cached)
            diff = {
                "input": tot_fresh - inc["input"],
                "output": last_total.get("output_tokens", 0) - inc["output"],
                "cache_read": tot_cached - inc["cache_read"],
            }
            if any(v > 0 for v in diff.values()):
                b = bucket(last_day)
                for k, v in diff.items():
                    if v > 0:
                        b[k] += v
    log(f"codex: {n_events} token events across {len(files)} session files")
    return days


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

    # Codex: full re-scan is authoritative, overwrite all codex entries.
    for day, models in collect_codex().items():
        days.setdefault(day, {})["codex"] = {"models": models}

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
