#!/usr/bin/env python3
"""Render data/usage.json into assets/token-usage.svg.

Pure stdlib. Card contents:
  - GitHub-contributions-style heatmap of the last ~52 weeks (input+output/day)
  - Donut of model share over the last 30 days (input+output)
  - Today / yesterday / 7d / 30d totals, cache-read shown separately
"""

import json
import math
from datetime import date, datetime, timedelta
from pathlib import Path
from xml.sax.saxutils import escape

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_FILE = REPO_ROOT / "data" / "usage.json"
OUT_FILE = REPO_ROOT / "assets" / "token-usage.svg"

CELL, GAP = 10, 3
STEP = CELL + GAP
LEFT_LABELS = 28
PAD = 16
WEEKS = 53

MODEL_COLORS = [
    "#40c463", "#58a6ff", "#f778ba", "#e3b341",
    "#a371f7", "#79c0ff", "#ffa657", "#56d4dd",
    "#ff7b72", "#d2a8ff",
]

MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
          "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def fmt(n: float) -> str:
    n = float(n)
    for unit in ("", "K", "M", "B", "T"):
        if abs(n) < 1000 or unit == "T":
            s = f"{n:.1f}" if unit else f"{int(n)}"
            if s.endswith(".0"):
                s = s[:-2]
            return f"{s}{unit}"
        n /= 1000
    return str(n)


def day_totals(days: dict) -> dict:
    """{day: (processed, fresh)} summed over sources/models.

    processed = input + output + cache_read (total tokens processed);
    fresh     = input + output only.
    """
    out = {}
    for day, sources in days.items():
        proc = fresh = 0
        for src in sources.values():
            for u in (src.get("models") or {}).values():
                fo = u.get("input", 0) + u.get("output", 0)
                fresh += fo
                proc += fo + u.get("cache_read", 0)
        out[day] = (proc, fresh)
    return out


def model_totals(days: dict, since: date) -> tuple:
    """({label: processed tokens}, cache_read) for days >= since."""
    totals, cache_read = {}, 0
    for day, sources in days.items():
        if date.fromisoformat(day) < since:
            continue
        for src_name, src in sources.items():
            if src_name == "codex":
                label_src = "Codex"
            elif src_name.startswith("codex-"):
                label_src = f"Codex · {src_name[len('codex-'):]}"
            else:
                label_src = {"cursor": "Cursor", "dsh-kimi": "DSH · Kimi"}.get(
                    src_name, src_name
                )
            for model, u in (src.get("models") or {}).items():
                label = f"{label_src} · {model}"
                totals[label] = (
                    totals.get(label, 0)
                    + u.get("input", 0)
                    + u.get("output", 0)
                    + u.get("cache_read", 0)
                )
                cache_read += u.get("cache_read", 0)
    return totals, cache_read


def level(t: int, max_t: int) -> int:
    if t <= 0 or max_t <= 0:
        return 0
    ratio = math.log10(t + 1) / math.log10(max_t + 1)
    return 1 + min(3, int(ratio * 4))


def render(data: dict) -> str:
    days = data.get("days") or {}
    totals = day_totals(days)
    today = date.today()
    # grid: last WEEKS columns, weeks start on Sunday, rightmost col = current week
    start = today - timedelta(days=(WEEKS * 7 - 1))
    start -= timedelta(days=(start.weekday() + 1) % 7)  # back to Sunday

    max_t = max((t for t, _ in totals.values()), default=0)

    cells, month_labels = [], []
    prev_month = None
    for w in range(WEEKS):
        col_x = PAD + LEFT_LABELS + w * STEP
        col_start = start + timedelta(days=w * 7)
        if col_start.day <= 7 and col_start.month != prev_month:
            month_labels.append(
                f'<text x="{col_x}" y="{PAD + 30}" class="muted small">{MONTHS[col_start.month - 1]}</text>'
            )
            prev_month = col_start.month
        for d in range(7):
            day = col_start + timedelta(days=d)
            if day > today:
                continue
            key = day.isoformat()
            t, fresh = totals.get(key, (0, 0))
            lv = level(t, max_t)
            y = PAD + 40 + d * STEP
            if t:
                tip = f"{key}: {fmt(t)} tokens processed (fresh in+out {fmt(fresh)})"
            else:
                tip = f"{key}: no usage"
            cells.append(
                f'<rect x="{col_x}" y="{y}" width="{CELL}" height="{CELL}" rx="2" class="lv{lv}">'
                f"<title>{escape(tip)}</title></rect>"
            )

    grid_w = WEEKS * STEP
    dow_labels = "".join(
        f'<text x="{PAD}" y="{PAD + 40 + i * STEP + 9}" class="muted small">{s}</text>'
        for i, s in ((1, "Mon"), (3, "Wed"), (5, "Fri"))
    )

    # ---- stats
    def get(d): return totals.get(d.isoformat(), (0, 0))[0]
    today_t = get(today)
    yday_t = get(today - timedelta(days=1))
    t7 = sum(get(today - timedelta(days=i)) for i in range(7))
    t30 = sum(get(today - timedelta(days=i)) for i in range(30))

    since30 = today - timedelta(days=29)
    mt, cache30 = model_totals(days, since30)
    mt = dict(sorted(mt.items(), key=lambda kv: -kv[1]))
    mt_total = sum(mt.values())
    # fold tiny slices (<1%) into "Other" to keep the legend readable
    big = {k: v for k, v in mt.items() if mt_total and v / mt_total >= 0.01}
    small_sum = sum(v for k, v in mt.items() if k not in big)
    mt = big
    if small_sum:
        mt["Other"] = small_sum

    # ---- donut
    heat_bottom = PAD + 40 + 7 * STEP
    cx, cy, r, sw = PAD + 76, heat_bottom + 86, 52, 20
    circ = 2 * math.pi * r
    donut, legend = [f'<circle cx="{cx}" cy="{cy}" r="{r}" fill="none" class="track" stroke-width="{sw}"/>'], []
    offset = 0.0
    ly = heat_bottom + 30
    for i, (label, v) in enumerate(mt.items()):
        color = MODEL_COLORS[i % len(MODEL_COLORS)]
        frac = v / mt_total if mt_total else 0
        seg = max(frac * circ - 1.5, 0)
        donut.append(
            f'<circle cx="{cx}" cy="{cy}" r="{r}" fill="none" stroke="{color}" stroke-width="{sw}"'
            f' stroke-dasharray="{seg:.2f} {circ - seg:.2f}" stroke-dashoffset="{-offset:.2f}"'
            f' transform="rotate(-90 {cx} {cy})"><title>{escape(label)}: {fmt(v)} ({frac * 100:.1f}%)</title></circle>'
        )
        offset += frac * circ
        pct = f"{frac * 100:.1f}%" if frac >= 0.001 else "<0.1%"
        legend.append(
            f'<rect x="{PAD + 170}" y="{ly - 9}" width="10" height="10" rx="2" fill="{color}"/>'
            f'<text x="{PAD + 186}" y="{ly}" class="text small">{escape(label)}'
            f' · {escape(pct)}</text>'
        )
        ly += 18
    donut.append(
        f'<text x="{cx}" y="{cy - 2}" text-anchor="middle" class="strong mid">{fmt(mt_total)}</text>'
        f'<text x="{cx}" y="{cy + 14}" text-anchor="middle" class="muted small">30 days</text>'
    )

    # ---- right-hand stats block
    sx = PAD + 170 + 320
    stats = "".join(
        f'<text x="{sx}" y="{y}" class="text small">{escape(k)}</text>'
        f'<text x="{sx + 190}" y="{y}" text-anchor="end" class="strong small">{escape(v)}</text>'
        for y, (k, v) in zip(
            range(heat_bottom + 30, heat_bottom + 30 + 6 * 22, 22),
            [
                ("Today", f"{fmt(today_t)} tokens"),
                ("Yesterday", f"{fmt(yday_t)} tokens"),
                ("Last 7 days", f"{fmt(t7)} tokens"),
                ("Last 30 days", f"{fmt(mt_total)} tokens"),
                ("Cache read (30d)", f"{fmt(cache30)} tokens"),
            ],
        )
    )
    stats += (
        f'<text x="{sx}" y="{heat_bottom + 30 + 5 * 22}" class="muted small">'
        f"total tokens processed · cache reads included</text>"
    )

    width = max(PAD * 2 + LEFT_LABELS + grid_w, sx + 200)
    height = heat_bottom + 86 + 70 + PAD

    return f'''<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}" role="img" aria-label="Daily token usage">
<style>
  .bg {{ fill: #ffffff; stroke: #d0d7de; }}
  .strong {{ fill: #1f2328; font: 600 14px -apple-system, "Segoe UI", Helvetica, Arial, sans-serif; }}
  .text {{ fill: #1f2328; font: 400 12px -apple-system, "Segoe UI", Helvetica, Arial, sans-serif; }}
  .muted {{ fill: #57606a; font: 400 12px -apple-system, "Segoe UI", Helvetica, Arial, sans-serif; }}
  .small {{ font-size: 10px; }} .mid {{ font-size: 13px; }}
  .lv0 {{ fill: #ebedf0; }} .lv1 {{ fill: #9be9a8; }} .lv2 {{ fill: #40c463; }}
  .lv3 {{ fill: #30a14e; }} .lv4 {{ fill: #216e39; }}
  .track {{ stroke: #ebedf0; }}
  @media (prefers-color-scheme: dark) {{
    .bg {{ fill: #0d1117; stroke: #30363d; }}
    .strong {{ fill: #e6edf3; }} .text {{ fill: #e6edf3; }} .muted {{ fill: #8b949e; }}
    .lv0 {{ fill: #161b22; }} .lv1 {{ fill: #0e4429; }} .lv2 {{ fill: #006d32; }}
    .lv3 {{ fill: #26a641; }} .lv4 {{ fill: #39d353; }}
    .track {{ stroke: #161b22; }}
  }}
</style>
<rect class="bg" x="0.5" y="0.5" width="{width - 1}" height="{height - 1}" rx="6"/>
<text x="{PAD}" y="{PAD + 12}" class="strong">⚡ Daily Token Usage</text>
<text x="{width - PAD}" y="{PAD + 12}" text-anchor="end" class="muted small">updated {escape((data.get("updated_at") or "")[:16].replace("T", " "))}</text>
{"".join(month_labels)}
{dow_labels}
{"".join(cells)}
{"".join(donut)}
{"".join(legend)}
{stats}
</svg>
'''


def main():
    data = json.loads(DATA_FILE.read_text(encoding="utf-8"))
    OUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    svg = render(data)
    # fail loudly rather than publishing a broken image
    import xml.etree.ElementTree as ET

    ET.fromstring(svg)
    OUT_FILE.write_text(svg, encoding="utf-8")
    print(f"[render] wrote {OUT_FILE.relative_to(REPO_ROOT)}")


if __name__ == "__main__":
    main()
