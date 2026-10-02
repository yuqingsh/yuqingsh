#!/usr/bin/env python3
"""Self-contained Codex token-usage scanner (stdlib only, py3.9+).

Scans <base>/sessions and <base>/archived_sessions for token_count events and
prints a JSON object to stdout:
  {"days": {day: {model: {"input","output","cache_read"}}}, "events": N, "files": M}

Used two ways by tools/collect_usage.py:
  - locally:  imported and called via scan_codex()
  - remotely: streamed over `ssh HOST python3 -` (no remote installation)

Day boundaries use a fixed UTC+8 so local and remote scans agree.
"""

import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

TZ = timezone(timedelta(hours=8))  # fixed day boundary, matches the user's TZ


def local_day_from_iso(ts):
    dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    return dt.astimezone(TZ).strftime("%Y-%m-%d")


def model_name(base: Path) -> str:
    try:
        text = (base / "config.toml").read_text(encoding="utf-8")
        m = re.search(r'^\s*model\s*=\s*"([^"]+)"', text, re.M)
        if m:
            return m.group(1)
    except OSError:
        pass
    return "codex"


def scan_codex(base: Path) -> dict:
    """Scan base/{sessions,archived_sessions}; return {day: {model: usage}}.

    Sums last_token_usage deltas (tokens actually processed per API call).
    total_token_usage is deliberately NOT used for reconciliation: forked /
    resumed sessions of one thread share a single cumulative odometer, so
    mixing it in would multiply-count entire conversation histories.
    """
    days = {}
    files = []
    for sub in ("sessions", "archived_sessions"):
        d = base / sub
        if d.is_dir():
            files.extend(d.rglob("*.jsonl"))
    model = model_name(base)
    n_events = 0

    def bucket(day):
        return days.setdefault(day, {}).setdefault(
            model, {"input": 0, "output": 0, "cache_read": 0}
        )

    for path in sorted(files):
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
                    if not usage:
                        continue
                    day = local_day_from_iso(d["timestamp"])
                    # OpenAI semantics: input_tokens INCLUDES cached_input_tokens
                    cached = usage.get("cached_input_tokens", 0)
                    fresh = max(0, usage.get("input_tokens", 0) - cached)
                    out = usage.get("output_tokens", 0)
                    b = bucket(day)
                    b["input"] += fresh
                    b["output"] += out
                    b["cache_read"] += cached
                    n_events += 1
        except OSError:
            continue
    return {"days": days, "events": n_events, "files": len(files), "model": model}


def main():
    base = Path(sys.argv[1]).expanduser() if len(sys.argv) > 1 else Path.home() / ".codex"
    result = scan_codex(base)
    print(json.dumps(result))


if __name__ == "__main__":
    main()
