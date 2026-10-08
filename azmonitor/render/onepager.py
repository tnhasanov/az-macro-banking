"""The monthly edition on one A4 page: headline numbers, two trend charts, three things to know.

Built from the edition's own fact pack and narrative. Every figure on the page is a value the
fact pack already carries — a scorecard metric with its period and comparison, a chart series,
the policy record, a triggered watch flag — or a finding the narrative already states, so the
page adds no number the deck does not already show. Nothing here computes a new statistic.

Periods are never assumed to agree. Banking data, prices, GDP and policy decisions are published
on different calendars, so every tile, chart and sentence carries the period of its own figure.
A policy decision whose figures are not yet confirmed is shown as exactly that: the page then
states the last confirmed rate and the date it was set, and never carries an older decision's
figures forward as if they were the new one's.

Rendered as HTML with inline SVG and printed by headless Chromium to a PDF (A4) and a PNG
(for an email body or a chat). Fonts: Open Sans, the package the report runners install, with
Noto Sans and Liberation Sans behind it, so local and production renders agree. Before printing,
the layout is measured in the same browser: content that would run off the page or be clipped
inside a card is reported, not silently cut.

Colour: five theme hues from the documented categorical palette (validated for CVD separation
and the normal-vision floor; two sit below 3:1 on white, so every chart line is direct-labelled
and every tile states its value in text). Text is always ink, never a series colour; a change is
coloured only where the scorecard says which direction is good, and always carries an arrow.
"""
from __future__ import annotations

import html
import json
import math
import os
import re
import shutil
import subprocess
import tempfile
from datetime import date
from pathlib import Path
from typing import Any

# 16px line icons, drawn with the theme colour as stroke
ICONS = {
    "economy": '<path d="M2.5 13.5h11"/><path d="M4.5 11V8.5M8 11V5.5M11.5 11V3"/>',
    "prices": ('<path d="M8.6 2.5H13a.5.5 0 0 1 .5.5v4.4a1 1 0 0 1-.3.7l-5.6 5.6a1 1 0 0 1-1.4 0L2.3 9.8a1 1 0 0 1 '
               '0-1.4l5.6-5.6a1 1 0 0 1 .7-.3z"/><circle cx="10.8" cy="5.2" r="1"/>'),
    "lending": ('<ellipse cx="8" cy="4.5" rx="4.5" ry="2"/><path d="M3.5 4.5V8c0 1.1 2 2 4.5 2s4.5-.9 4.5-2V4.5"/>'
                '<path d="M3.5 8v3.5c0 1.1 2 2 4.5 2s4.5-.9 4.5-2V8"/>'),
    "funding": '<path d="M2 6.5 8 3l6 3.5"/><path d="M3.8 7.5v4.5M6.6 7.5v4.5M9.4 7.5v4.5M12.2 7.5v4.5"/><path d="M2 13.5h12"/>',
    "health": '<path d="M8 2l5 2v4c0 3-2.2 5.2-5 6-2.8-.8-5-3-5-6V4z"/><path d="M5.8 8.2l1.6 1.6 3-3.2"/>',
}
THEMES = {
    "economy": {"label": "Economy", "colour": "#2a78d6", "tint": "#eaf2fc"},
    "prices": {"label": "Prices & policy", "colour": "#eb6834", "tint": "#fdeee8"},
    "lending": {"label": "Lending", "colour": "#1baf7a", "tint": "#e6f6f0"},
    "funding": {"label": "Funding", "colour": "#4a3aa7", "tint": "#edebf7"},
    "health": {"label": "Bank health", "colour": "#e87ba4", "tint": "#fcedf3"},
}
INK, INK_2, MUTED = "#0b0b0b", "#52514e", "#898781"
GRID, AXIS, SURFACE = "#e1e0d9", "#c3c2b7", "#ffffff"
GOOD, BAD = "#006300", "#b42828"
WARNING = "#fab219"                       # status palette: always paired with an icon and a label
POLICY_INK = "#3d3c39"

MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
MONTHS_LONG = ["January", "February", "March", "April", "May", "June", "July", "August",
               "September", "October", "November", "December"]

# Plain names for the metrics a finding or a flag can point at; the pack's own label is the fallback.
NAMES = {
    "cba.bank.liquid_assets_ratio": ("Liquid assets", "Liquid assets were {v}% of bank assets"),
    "cba.bank.equity_to_assets": ("Bank capital", "Bank capital was {v}% of assets"),
    "cba.loans.overdue_ratio": ("Overdue loans", "Overdue loans were {v}% of all loans"),
    "cba.rates.new.spread": ("The AZN pricing spread", "New AZN loans were priced {v} pp above new AZN term deposits"),
    "ssc.real_wage_growth_est": ("Real wage growth", "Real wages grew an estimated {v}% (year to date)"),
    "ssc.hl.gdp.growth": ("GDP growth", "GDP grew {v}% in real terms (year to date)"),
    "cba.loans.total_ci.yoy": ("Loan growth", "Loans to the economy grew {v}% y/y"),
    "cba.deposits.total.yoy": ("Deposit growth", "Deposits grew {v}% y/y"),
    "cba.deposits.fx_share": ("FX share of deposits", "{v}% of deposits were in foreign currency"),
    "cba.bank.npl.ratio": ("Non-performing loans", "Non-performing loans were {v}% of bank loans"),
    "ssc.cpi.all.yoy": ("Inflation", "Consumer prices rose {v}% y/y"),
}


# ------------------------------------------------------------------------------ reading the pack

def _metric_by_key(fp: dict, key: str) -> dict | None:
    m = (fp.get("metrics") or {}).get(key)
    return m if isinstance(m, dict) and m.get("available", True) and m.get("latest") else None


def _metric_by_id(fp: dict, metric_id: str) -> dict | None:
    for m in (fp.get("metrics") or {}).values():
        if isinstance(m, dict) and m.get("id") == metric_id and m.get("latest"):
            return m
    return None


def _series(fp: dict, series_id: str, dims: dict | None = None) -> list[tuple[str, float]]:
    """A chart series the deck already plots, by id: [(period_end, value), ...]."""
    for slide in (fp.get("slides") or {}).values():
        if not isinstance(slide, dict):
            continue
        for key, val in slide.items():
            if not key.startswith("chart") or not isinstance(val, list):
                continue
            for s in val:
                if isinstance(s, dict) and s.get("id") == series_id and (dims is None or s.get("dims") == dims):
                    pts = [(str(p)[:10], float(v)) for p, v in (s.get("points") or [])
                           if v is not None and not (isinstance(v, float) and math.isnan(v))]
                    return sorted(pts)
    return []


def _policy(fp: dict) -> dict:
    pubs = fp.get("publications")
    return (pubs.get("policy") or {}) if isinstance(pubs, dict) else {}


def _good(key: str) -> str:
    """up | down | neutral, from the scorecard definition the deck uses."""
    try:
        from .. import config
        for row in config.reports_config()["monthly"]["scorecard"]:
            if f"scorecard.{row['key']}" == key:
                return row.get("good", "neutral")
    except Exception:
        pass
    return "neutral"


def policy_view(fp: dict) -> dict[str, Any]:
    """The refinancing rate in force and the latest decision, kept apart when they differ.

    The rate path holds the confirmed level decided at each meeting. The latest decision can be
    newer than the last confirmed level (its figures arrive with a later publication); it is then
    reported as pending, and the confirmed level is shown with the date it was set.
    """
    pol = _policy(fp)
    dec = pol.get("decision") or {}
    path = [(str(p["date"])[:10], float(p["value"])) for p in (pol.get("rate_path") or [])
            if p.get("value") is not None]
    latest = str(dec.get("announcement_date") or "")[:10] or None
    view: dict[str, Any] = {"path": path, "rate": None, "set_on": None, "latest_decision": latest, "pending": None,
                            "change_bp": None, "corridor": None}
    if not path:
        return view
    view["set_on"], view["rate"] = path[-1]
    if latest and latest > view["set_on"] and dec.get("policy_rate") is None:
        view["pending"] = latest
    elif latest == view["set_on"] and dec.get("policy_rate") is not None:
        view["change_bp"] = dec.get("rate_change_bp")
        if dec.get("corridor_floor") is not None and dec.get("corridor_ceiling") is not None:
            view["corridor"] = (float(dec["corridor_floor"]), float(dec["corridor_ceiling"]))
    return view


# ------------------------------------------------------------------------------ formatting

def _d(iso: str) -> date:
    return date.fromisoformat(str(iso)[:10])


def _month_short(iso: str) -> str:
    d = _d(iso)
    return f"{MONTHS[d.month - 1]} {d.year}"


def _day(iso: str, year: bool = True) -> str:
    d = _d(iso)
    return f"{d.day} {MONTHS[d.month - 1]}" + (f" {d.year}" if year else "")


def _period(metric: dict, which: str = "latest") -> str:
    """A period label from the metric's own period type: Jan–Aug 2026 for a year-to-date figure,
    end-Aug 2026 for a month-end stock, Aug 2026 for a monthly reading."""
    obs = metric.get(which) or {}
    if not obs.get("period"):
        return ""
    d, kind = _d(obs["period"]), str(metric.get("period_type") or "")
    if "ytd" in kind:
        return f"Jan–{MONTHS[d.month - 1]} {d.year}" if d.month > 1 else f"Jan {d.year}"
    return obs.get("period_label") or _month_short(obs["period"])


def _num(v: float, decimals: int = 1) -> str:
    s = f"{abs(v):,.{decimals}f}"
    return ("−" if round(v, decimals) < 0 else "") + s


def _signed(v: float, decimals: int = 1) -> str:
    if round(v, decimals) == 0:
        return f"{0:.{decimals}f}"
    return ("+" if v > 0 else "−") + f"{abs(v):,.{decimals}f}"


def _esc(s: Any) -> str:
    return html.escape(str(s), quote=True)


_MON = "Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec"


def _text(s: Any) -> str:
    """Escaped text in which a figure stays on one line with its unit, and a period with its year."""
    out = re.sub(r"(\d) (pp|bp|mln)\b", r"\1&nbsp;\2", _esc(s))
    out = re.sub(r"\bmln AZN\b", "mln&nbsp;AZN", out)
    return re.sub(rf"\b((?:end-)?(?:{_MON})(?:–(?:{_MON}))?) (\d{{4}})\b", r'<span class="nw">\1&nbsp;\2</span>', out)


def _luminance(hex_colour: str) -> float:
    rgb = [int(hex_colour.lstrip("#")[i:i + 2], 16) / 255 for i in (0, 2, 4)]
    lin = [c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4 for c in rgb]
    return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]


def _on(fill: str) -> str:
    """White or ink for text set inside a coloured fill, whichever contrasts more."""
    lum = _luminance(fill)
    return "#ffffff" if (1.05 / (lum + 0.05)) >= ((lum + 0.05) / (_luminance(INK) + 0.05)) else INK


# ------------------------------------------------------------------------------ SVG pieces

def _scale(lo: float, hi: float, a: float, b: float):
    span = (hi - lo) or 1.0
    return lambda v: a + (v - lo) / span * (b - a)


def _nice_ticks(lo: float, hi: float, n: int = 4) -> list[float]:
    span = max(hi - lo, 1e-9)
    raw = span / n
    mag = 10 ** math.floor(math.log10(raw))
    step = min((s * mag for s in (1, 2, 2.5, 5, 10)), key=lambda s: abs(span / s - n))
    start = math.floor(lo / step) * step
    ticks, t = [], start
    while t <= hi + step * 0.5:
        ticks.append(round(t, 10))
        t += step
    return ticks


def _path(points: list[tuple[float, float]], step: bool = False) -> str:
    if not points:
        return ""
    out = [f"M{points[0][0]:.1f},{points[0][1]:.1f}"]
    for x1, y1 in points[1:]:
        out.append(f"H{x1:.1f}V{y1:.1f}" if step else f"L{x1:.1f},{y1:.1f}")
    return "".join(out)


def _icon(theme: str, size: int = 12) -> str:
    return (f'<svg width="{size}" height="{size}" viewBox="0 0 16 16" fill="none" stroke="{THEMES[theme]["colour"]}" '
            f'stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">{ICONS[theme]}</svg>')


def _sparkline(values: list[float], colour: str, w: int = 150, h: int = 30, step: bool = False) -> str:
    if len(values) < 2:
        return ""
    lo, hi = min(values), max(values)
    pad = (hi - lo) * 0.15 or 0.5
    sx = _scale(0, len(values) - 1, 3, w - 6)
    sy = _scale(lo - pad, hi + pad, h - 4, 4)
    pts = [(sx(i), sy(v)) for i, v in enumerate(values)]
    line = _path(pts, step)
    area = line + f"L{pts[-1][0]:.1f},{h}L{pts[0][0]:.1f},{h}Z"
    x, y = pts[-1]
    return (f'<svg class="spark" width="{w}" height="{h}" viewBox="0 0 {w} {h}" aria-hidden="true">'
            f'<path d="{area}" fill="{colour}" fill-opacity="0.10"/>'
            f'<path d="{line}" fill="none" stroke="{colour}" stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>'
            f'<circle cx="{x:.1f}" cy="{y:.1f}" r="4" fill="{colour}" stroke="{SURFACE}" stroke-width="2"/></svg>')


def _arrow(direction: int, colour: str) -> str:
    if direction > 0:
        shape = '<path d="M5 1.5 L9 8 H1 Z"/>'
    elif direction < 0:
        shape = '<path d="M5 8.5 L9 2 H1 Z"/>'
    else:
        shape = '<rect x="1.5" y="3.5" width="7" height="3" rx="1.5"/>'
    return f'<svg width="9" height="9" viewBox="0 0 10 10" fill="{colour}" aria-hidden="true">{shape}</svg>'


def _info(colour: str) -> str:
    return (f'<svg width="10" height="10" viewBox="0 0 10 10" aria-hidden="true"><circle cx="5" cy="5" r="4.2" fill="none" '
            f'stroke="{colour}" stroke-width="1.2"/><path d="M5 4.4v2.8" stroke="{colour}" stroke-width="1.3" '
            f'stroke-linecap="round"/><circle cx="5" cy="2.9" r=".7" fill="{colour}"/></svg>')


def _flag_icon() -> str:
    return (f'<svg width="14" height="14" viewBox="0 0 16 16" aria-hidden="true"><path d="M3.5 14.5V2" stroke="{INK}" '
            f'stroke-width="1.6" stroke-linecap="round"/><path d="M4 2.5h8.5l-2 3.25 2 3.25H4z" fill="{WARNING}" '
            f'stroke="{INK}" stroke-width="1.2" stroke-linejoin="round"/></svg>')


def _month_ticks(dates: list[str], every: int = 6) -> list[str]:
    """Every sixth month counting back from the latest, so the last label is the latest month."""
    if not dates:
        return []
    return sorted(dates[i] for i in range(len(dates) - 1, -1, -every))


def _line_chart(series: list[dict], *, w: int, h: int, ticks_from: list[str]) -> str:
    """Up to two series on one % axis. Each: {label, short, points, colour, step, decimals}."""
    series = [s for s in series if len(s["points"]) >= 2]
    if not series:
        return ""
    xs = sorted({p[0] for s in series for p in s["points"]})
    x0, x1 = _d(xs[0]).toordinal(), _d(xs[-1]).toordinal()
    vals = [v for s in series for _, v in s["points"]]
    lo, hi = min(0.0, min(vals)), max(vals)
    ticks = _nice_ticks(lo, hi * 1.06 if hi > 0 else hi + 1, 4)
    lo, hi = min(ticks[0], lo), ticks[-1]
    left, right, top, bottom = 26, 64, 8, 20
    sx = _scale(x0, x1, left + 4, w - right)
    sy = _scale(lo, hi, h - bottom, top)
    parts = [f'<svg class="chart" width="{w}" height="{h}" viewBox="0 0 {w} {h}" role="img">']
    for t in ticks:
        y = sy(t)
        parts.append(f'<line x1="{left}" x2="{w - right + 4}" y1="{y:.1f}" y2="{y:.1f}" stroke="{AXIS if t == 0 else GRID}" stroke-width="1"/>')
        parts.append(f'<text x="{left - 5}" y="{y + 3:.1f}" class="tick" text-anchor="end">{_esc(_num(t, 0 if float(t).is_integer() else 1))}</text>')
    for iso in _month_ticks(ticks_from):
        d = _d(iso)
        parts.append(f'<text x="{sx(d.toordinal()):.1f}" y="{h - 5}" class="tick" text-anchor="middle">'
                     f'{MONTHS[d.month - 1]} ’{str(d.year)[2:]}</text>')
    ends = []
    for s in series:
        pts = [(sx(_d(p).toordinal()), sy(v)) for p, v in s["points"]]
        parts.append(f'<path d="{_path(pts, s.get("step", False))}" fill="none" stroke="{s["colour"]}" stroke-width="2" '
                     f'stroke-linejoin="round" stroke-linecap="round"/>')
        ends.append((pts[-1], s))
    # end dots and direct labels; when two would collide the lower one moves down on a leader line
    ends.sort(key=lambda e: e[0][1])
    placed: list[float] = []
    for (x, y), s in ends:
        ly = y
        for p in placed:
            if abs(ly - p) < 24:
                ly = p + 24
        ly = min(ly, h - bottom - 8)
        placed.append(ly)
        parts.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="4" fill="{s["colour"]}" stroke="{SURFACE}" stroke-width="2"/>')
        if abs(ly - y) > 2:
            parts.append(f'<path d="M{x + 5:.1f},{y:.1f} L{x + 9:.1f},{ly:.1f}" stroke="{AXIS}" stroke-width="1" fill="none"/>')
        value = s["points"][-1][1]
        parts.append(f'<text x="{x + 11:.1f}" y="{ly + 1:.1f}" class="end-value">{_esc(_num(value, s.get("decimals", 1)))}%</text>')
        parts.append(f'<text x="{x + 11:.1f}" y="{ly + 12:.1f}" class="end-label">{_esc(s["short"])}</text>')
    parts.append("</svg>")
    return "".join(parts)


def _legend(series: list[dict]) -> str:
    items = "".join(
        f'<span class="key"><svg width="16" height="8" aria-hidden="true"><line x1="1" x2="15" y1="4" y2="4" '
        f'stroke="{s["colour"]}" stroke-width="2.5" stroke-linecap="round"/></svg>{_esc(s["label"])}</span>'
        for s in series)
    return f'<div class="legend">{items}</div>'


# ------------------------------------------------------------------------------ the page's parts

def _change_chip(metric: dict, decimals: int, unit: str = "pp") -> tuple[str, int] | None:
    change = metric.get("change")
    if change is None or (isinstance(change, float) and math.isnan(change)):
        return None
    cd = round(float(change), decimals)
    direction = 0 if cd == 0 else (1 if cd > 0 else -1)
    text = f"{_signed(cd, decimals)} {unit}".strip()
    prior = _period(metric, "prior").replace("end-", "")
    latest_year = str(_d(metric["latest"]["period"]).year)
    if prior:
        if prior.endswith(latest_year) and metric.get("compare") != "lag12":
            prior = prior[: -len(latest_year)].strip()      # same year as the period shown just above
        text += f" vs {prior}"
    return text, direction


def _tone(direction: int, good: str) -> str:
    if not direction or good not in ("up", "down"):
        return "neutral"
    return "good" if (direction > 0) == (good == "up") else "bad"


def _rate_by_month(path: list[tuple[str, float]], months: list[str]) -> list[float]:
    """The confirmed rate in force at each month end, read off the decision path."""
    out = []
    for m in months:
        in_force = [v for d, v in path if d <= m]
        if in_force:
            out.append(in_force[-1])
    return out


def _rate_points(pv: dict, window: list[tuple[str, float]]) -> list[tuple[str, float]]:
    """The confirmed rate as a step series over a chart's window: the level in force at the start,
    each decision inside it, and the last level carried to the window's end only when no newer
    decision with unconfirmed figures falls inside the window."""
    if not window or not pv["path"]:
        return []
    start, end = window[0][0], window[-1][0]
    before = [p for p in pv["path"] if p[0] <= start]
    pts = ([(start, before[-1][1])] if before else []) + [p for p in pv["path"] if start < p[0] <= end]
    if pts and pts[-1][0] < end and not (pv["pending"] and pv["pending"] <= end):
        pts.append((end, pts[-1][1]))
    return pts


def _tiles(fp: dict) -> list[dict]:
    """The headline numbers, each with its own period, comparison and recent trend."""
    out: list[dict] = []

    def add(key: str, theme: str, title: str, sub: str, series: list[tuple[str, float]], *, decimals: int = 1,
            extra: str = ""):
        m = _metric_by_key(fp, key)
        if not m:
            return
        chip = _change_chip(m, decimals)
        out.append({"theme": theme, "title": title, "sub": sub, "value": f"{_num(float(m['latest']['value']), decimals)}%",
                    "unit": "", "period": _period(m), "chip": chip[0] if chip else "", "dir": chip[1] if chip else 0,
                    "tone": _tone(chip[1], _good(key)) if chip else "neutral", "extra": extra, "bars": "",
                    "chip_icon": "arrow", "spark": _sparkline([v for _, v in series[-24:]], THEMES[theme]["colour"])})

    gdp = _metric_by_key(fp, "scorecard.gdp_growth")
    add("scorecard.non_oil_growth", "economy", "Non-oil GDP growth", "real, year to date, y/y",
        _series(fp, "ssc.hl.gdp_nonoil.growth"),
        extra=f"Total GDP {_num(float(gdp['latest']['value']))}% ({_period(gdp)})" if gdp else "")
    add("scorecard.cpi_yoy", "prices", "Inflation", "consumer prices, y/y", _series(fp, "ssc.cpi.all.yoy"))

    pv = policy_view(fp)
    if pv["rate"] is not None:
        monthly = _rate_by_month(pv["path"], [p for p, _ in _series(fp, "ssc.cpi.all.yoy")[-24:]])
        extra, extra_icon, icon = "", "", "arrow"
        if pv["pending"]:
            chip, direction = "", 0
            extra, extra_icon = f"Figures from the {_day(pv['pending'], year=False)} decision are pending", "info"
        else:
            bp = pv["change_bp"]
            if bp is None:
                chip, direction = "", 0
            elif round(bp) == 0:
                chip, direction = "Unchanged", 0
            else:
                chip, direction = f"{'Raised' if bp > 0 else 'Cut'} {abs(bp):.0f} bp", (1 if bp > 0 else -1)
            if pv["corridor"]:
                extra = f"Corridor {_num(pv['corridor'][0], 2)}–{_num(pv['corridor'][1], 2)}%"
        out.append({"theme": "prices", "title": "CBA policy rate", "sub": "refinancing rate",
                    "value": f"{_num(pv['rate'], 2)}%", "unit": "", "period": f"decided {_day(pv['set_on'])}",
                    "chip": chip, "dir": direction, "tone": "neutral", "extra": extra, "extra_icon": extra_icon,
                    "bars": "", "chip_icon": icon, "spark": _sparkline(monthly, THEMES["prices"]["colour"], step=True)})

    add("scorecard.loans_yoy", "lending", "Loans to the economy", "growth, y/y", _series(fp, "cba.loans.total_ci.yoy"))
    add("scorecard.deposits_yoy", "funding", "Deposits", "growth, y/y", _series(fp, "cba.deposits.total.yoy"))
    add("scorecard.deposit_fx_share", "funding", "FX share of deposits", "foreign-currency deposits, % of all",
        _series(fp, "cba.deposits.fx_share"))
    add("scorecard.npl_ratio", "health", "Non-performing loans", "% of bank loans", _series(fp, "cba.bank.npl.ratio"))

    profit = _metric_by_key(fp, "scorecard.net_profit")
    if profit and profit.get("prior"):
        now, then = float(profit["latest"]["value"]), float(profit["prior"]["value"])
        top = max(now, then, 1e-9)
        row = ('<div class="bar-row"><span class="bar-label">{label}</span><span class="bar"><i style="width:{w:.1f}%;'
               'background:{c}"></i></span><span class="bar-value">{v}</span></div>')
        bars = ('<div class="bars">'
                + row.format(label=_esc(_period(profit, "prior")), w=then / top * 100, c="#c9c7bf", v=_esc(_num(then)))
                + row.format(label=_esc(_period(profit)), w=now / top * 100, c=THEMES["health"]["colour"], v=_esc(_num(now)))
                + '</div>')
        change = profit.get("change")
        direction = 0 if change is None or round(change, 1) == 0 else (1 if change > 0 else -1)
        out.append({"theme": "health", "title": "Bank net profit", "sub": "year to date",
                    "value": _num(now), "unit": "mln AZN", "period": _period(profit),
                    "chip": f"{_signed(float(change))} mln AZN" if change is not None else "", "dir": direction,
                    "tone": _tone(direction, _good("scorecard.net_profit")), "extra": "", "bars": bars,
                    "chip_icon": "arrow", "spark": ""})
    return out


def _headline(fp: dict) -> str:
    """The month in a sentence or two: figures grouped by their own month, then the CBA decision
    when its figures are confirmed. Direction words follow the sign of the change."""
    clauses: list[tuple[str, str, str, str]] = []          # (subject, verb, value, month)
    for key, subject, up, down in (("scorecard.cpi_yoy", "inflation", "rose to", "eased to"),
                                   ("scorecard.loans_yoy", "loan growth", "picked up to", "slowed to"),
                                   ("scorecard.deposits_yoy", "deposit growth", "picked up to", "slowed to")):
        m = _metric_by_key(fp, key)
        if not m or m.get("change") is None:
            continue
        c = round(float(m["change"]), 1)
        verb = up if c > 0 else down if c < 0 else "held at"
        d = _d(m["latest"]["period"])
        clauses.append((subject, verb, f"<b>{_num(float(m['latest']['value']))}%</b>", MONTHS_LONG[d.month - 1]))
    sentences = []
    for month in dict.fromkeys(c[3] for c in clauses):
        parts, last_verb = [], None
        for subject, verb, value, _ in (c for c in clauses if c[3] == month):
            # "loan growth picked up to 14.5% and deposit growth to 11.1%": a repeated verb is said once
            parts.append(f"{subject} to {value}" if verb == last_verb and verb.endswith(" to") else f"{subject} {verb} {value}")
            last_verb = verb
        body = ", ".join(parts[:-1]) + (" and " if len(parts) > 1 else "") + parts[-1]
        sentences.append(f"In {month}, {body}.")
    pv = policy_view(fp)
    if pv["rate"] is not None and not pv["pending"] and pv["change_bp"] is not None:
        bp = round(pv["change_bp"])
        verb = "kept its policy rate at" if bp == 0 else ("raised its policy rate to" if bp > 0 else "cut its policy rate to")
        sentences.append(f"The CBA {verb} <b>{_num(pv['rate'], 2)}%</b> on {_day(pv['set_on'])}.")
    return " ".join(sentences)


def _theme_of(metric_id: str) -> str:
    if metric_id.startswith(("ssc.cpi", "cba.policy", "cba.rates")):
        return "prices"
    if metric_id.startswith("ssc."):
        return "economy"
    if metric_id.startswith("cba.deposits"):
        return "funding"
    if metric_id.startswith("cba.loans"):
        return "lending"
    return "health"


def _metric_sentence(m: dict) -> tuple[str, str] | None:
    """Title and sentence for a finding, from the metric's own latest value, change and periods."""
    name, template = NAMES.get(m.get("id", ""), ("", ""))
    if m.get("change") is None or not template:
        return None
    c = round(float(m["change"]), 1)
    if c == 0:
        return None
    word = "higher" if c > 0 else "lower"
    yearly = m.get("compare") == "lag12"
    title = f"{name} {word} than {'a year earlier' if yearly else 'a month earlier'}"
    text = (template.format(v=_num(float(m["latest"]["value"]))) + f" at {_period(m)}, {_num(abs(c))} pp {word} than "
            + ("a year earlier." if yearly else f"at {_period(m, 'prior')}."))
    return title, text


def _first_sentence(text: str, limit: int = 220) -> str:
    """A long statement is shortened to its first sentence (the full text is in the report);
    a single long sentence is kept whole rather than cut."""
    if len(text) <= limit:
        return text
    first = re.split(r"(?<=[.!?])\s+(?=[A-Z])", text.strip(), maxsplit=1)[0]
    return first


def _takeaways(fp: dict, nar: dict, tile_ids: set[str]) -> list[dict]:
    """Three things the tiles do not already say: the narrative's own findings first, then
    contrasts inside series the deck already plots."""
    out: list[dict] = []
    facts_only = (nar.get("mode") or "facts_only") == "facts_only"
    for f in sorted(nar.get("findings") or [], key=lambda f: f.get("rank", 99)):
        refs = f.get("metric_refs") or []
        if not refs or refs[0] in tile_ids:
            continue
        m = _metric_by_id(fp, refs[0])
        statement = f.get("statement")
        text = statement.get("text") if isinstance(statement, dict) else statement
        if not m or not text:
            continue
        made = _metric_sentence(m)
        if facts_only:
            if not made:
                continue
            title, text = made
        else:                                     # an analyst's own words, as written
            title = made[0] if made else (m.get("label") or "").split("(")[0].strip()
            text = _first_sentence(text)
        out.append({"theme": _theme_of(refs[0]), "title": title, "text": text})
        if len(out) >= 3:
            return out

    hh, nfc = _series(fp, "cba.deposits.hh.total.yoy"), _series(fp, "cba.deposits.nfc.total.yoy")
    if hh and nfc and hh[-1][0] == nfc[-1][0]:
        faster = hh[-1][1] > nfc[-1][1]
        out.append({"theme": "funding",
                    "title": "Household deposits outpace companies'" if faster else "Company deposits outpace households'",
                    "text": f"Household deposits grew {_num(hh[-1][1])}% y/y and companies' deposits "
                            f"{_num(nfc[-1][1])}% (end-{_month_short(hh[-1][0])})."})
    food, nonfood, services = (_series(fp, "ssc.cpi.food.yoy"), _series(fp, "ssc.cpi.nonfood.yoy"),
                               _series(fp, "ssc.cpi.services.yoy"))
    if food and nonfood and services and food[-1][0] == nonfood[-1][0] == services[-1][0]:
        trio = sorted([("food", food[-1][1]), ("services", services[-1][1]), ("non-food goods", nonfood[-1][1])],
                      key=lambda t: -t[1])
        lead = {"food": "Food prices", "services": "Prices of services", "non-food goods": "Prices of non-food goods"}
        out.append({"theme": "prices", "title": f"{lead[trio[0][0]]} rise fastest",
                    "text": f"{lead[trio[0][0]]} were up {_num(trio[0][1])}% y/y, against {_num(trio[1][1])}% for "
                            f"{trio[1][0]} and {_num(trio[2][1])}% for {trio[2][0]} ({_month_short(food[-1][0])})."})
    hh_fx = _series(fp, "cba.deposits.hh.fx_share")
    if len(hh_fx) >= 13 and round(hh_fx[-1][1] - hh_fx[-13][1], 1) != 0:
        lower = hh_fx[-1][1] < hh_fx[-13][1]
        out.append({"theme": "funding", "title": f"Households hold {'less' if lower else 'more'} in foreign currency",
                    "text": f"{_num(hh_fx[-1][1])}% of household deposits were in foreign currency at "
                            f"end-{_month_short(hh_fx[-1][0])}, {_num(abs(hh_fx[-1][1] - hh_fx[-13][1]))} pp "
                            f"{'lower' if lower else 'higher'} than a year earlier."})
    return out[:3]


def _signals(fp: dict) -> list[dict]:
    """The monitor's own watch flags that fired this edition: a prompt to check, not a risk score."""
    out = []
    for f in fp.get("flags") or []:
        if not f.get("triggered") or f.get("current") is None or f.get("comparison") is None:
            continue
        mid = f.get("metric", "")
        m = _metric_by_id(fp, mid) or {}
        name = NAMES[mid][0] if mid in NAMES else (m.get("label") or mid)
        out.append({"name": name, "theme": _theme_of(mid), "from": f"{_num(float(f['comparison']))}%",
                    "to": f"{_num(float(f['current']))}%", "months": int(f.get("window") or 0)})
    return out


# ------------------------------------------------------------------------------ the page

CSS = """
@page { size: A4; margin: 0; }
* { box-sizing: border-box; }
html, body { margin: 0; padding: 0; }
body { width: 210mm; height: 297mm; overflow: hidden; background: #f3f2ee; color: #0b0b0b;
       font-family: "Open Sans", "Noto Sans", "Liberation Sans", sans-serif; font-size: 10px; line-height: 1.38;
       -webkit-print-color-adjust: exact; print-color-adjust: exact; }
.page { position: relative; width: 210mm; height: 297mm; padding: 0 9mm; display: flex; flex-direction: column; }
.page > * { flex-shrink: 0; }
header { position: relative; margin: 0 -9mm; padding: 20px 9mm 0; color: #fff; overflow: hidden;
         background: linear-gradient(118deg, #0d1433 0%, #182766 52%, #22409c 100%); }
.blob { position: absolute; border-radius: 50%; }
.kicker { position: relative; font-size: 8.5px; font-weight: 700; letter-spacing: .18em; text-transform: uppercase; color: #9fbcf2; }
h1 { position: relative; margin: 5px 0 0; font-size: 19px; font-weight: 600; letter-spacing: -.005em; line-height: 1.25; }
.month { position: relative; margin: 0; font-size: 36px; font-weight: 800; letter-spacing: -.02em; line-height: 1.12; }
.strip { position: relative; display: flex; margin: 12px -9mm 0; padding: 7px 9mm 8px;
         background: rgba(8,12,36,.38); border-top: 1px solid rgba(255,255,255,.12); }
.strip span { font-size: 9px; color: #e9eefc; padding-right: 12px; margin-right: 12px; border-right: 1px solid rgba(255,255,255,.18); }
.strip span:last-child { border-right: 0; }
.strip b { display: block; font-size: 7.5px; font-weight: 700; letter-spacing: .14em; text-transform: uppercase; color: #9fbcf2; }
.headline { position: relative; margin: 11px 0 0; padding: 10px 14px 10px 18px; background: #fff; border-radius: 12px;
            border: 1px solid rgba(11,11,11,.07); font-size: 13px; line-height: 1.45; color: #1d1c1a; }
.headline:before { content: ""; position: absolute; left: 0; top: 9px; bottom: 9px; width: 4px; border-radius: 0 4px 4px 0;
                   background: linear-gradient(180deg, #2a78d6, #eb6834, #1baf7a, #4a3aa7, #e87ba4); }
.headline b { font-weight: 800; color: #0b0b0b; }
h2 { display: flex; align-items: center; gap: 8px; margin: 13px 0 7px; font-size: 9.5px; font-weight: 700;
     letter-spacing: .14em; text-transform: uppercase; color: #52514e; }
h2:after { content: ""; flex: 1; height: 1px; background: #dcdad3; }
.tiles { display: grid; grid-template-columns: repeat(4, 1fr); gap: 8px; }
.tile { position: relative; border-radius: 12px; padding: 9px 11px 8px 13px; border: 1px solid rgba(11,11,11,.07);
        overflow: hidden; display: flex; flex-direction: column; background: #fff; }
.tile:before { content: ""; position: absolute; left: 0; top: 0; bottom: 0; width: 4px; }
.th { display: flex; align-items: center; gap: 5px; font-size: 7.5px; font-weight: 700; letter-spacing: .12em;
      text-transform: uppercase; color: #52514e; }
.ic { display: inline-flex; align-items: center; justify-content: center; width: 17px; height: 17px; border-radius: 5px; background: #fff; }
.tile .title { margin-top: 5px; font-size: 11px; font-weight: 700; color: #0b0b0b; line-height: 1.2; }
.tile .sub { font-size: 8.5px; color: #6b6a66; }
.tile .value { margin-top: 2px; font-size: 26px; font-weight: 800; letter-spacing: -.02em; line-height: 1.08; }
.tile .value small { font-size: 10.5px; font-weight: 700; color: #52514e; margin-left: 4px; letter-spacing: 0; }
.tile .period { font-size: 8.5px; color: #6b6a66; }
.delta { display: inline-flex; align-items: center; gap: 4px; margin-top: 5px; padding: 2px 7px; border-radius: 999px;
         font-size: 8.5px; font-weight: 700; width: fit-content; max-width: 100%; line-height: 1.3; }
.delta.good { color: #006300; background: #e6f4e4; }
.delta.bad { color: #b42828; background: #fbe9e9; }
.delta.neutral { color: #52514e; background: #efeee9; }
.tile .extra { font-size: 8.5px; color: #52514e; margin-top: 4px; }
.tile .extra svg { vertical-align: -1px; }
.nw { white-space: nowrap; }
.tile .spark { margin-top: auto; padding-top: 5px; display: block; box-sizing: content-box; }
.bars { margin-top: auto; padding-top: 6px; display: grid; gap: 4px; }
.bar-row { display: grid; grid-template-columns: 58px 1fr 32px; align-items: center; gap: 5px; font-size: 8px; color: #52514e; }
.bar { height: 9px; border-radius: 0 4px 4px 0; overflow: hidden; }
.bar i { display: block; height: 100%; border-radius: 0 4px 4px 0; }
.bar-value { text-align: right; font-weight: 700; color: #0b0b0b; font-variant-numeric: tabular-nums; }
.charts { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; }
.card { background: #fff; border-radius: 12px; padding: 10px 12px 4px; border: 1px solid rgba(11,11,11,.07); }
.card h3 { margin: 0; font-size: 11.5px; font-weight: 700; }
.card .sub { font-size: 8.5px; color: #6b6a66; margin-top: 1px; }
.legend { display: flex; flex-wrap: wrap; gap: 3px 12px; margin: 5px 0 1px; font-size: 8.5px; color: #52514e; }
.key { display: inline-flex; align-items: center; gap: 5px; }
svg.chart { display: block; }
svg.chart text { font-family: inherit; }
.tick { font-size: 8px; fill: #898781; font-variant-numeric: tabular-nums; }
.end-value { font-size: 10.5px; font-weight: 800; fill: #0b0b0b; }
.end-label { font-size: 8px; fill: #52514e; }
.things { display: grid; grid-template-columns: repeat(3, 1fr); gap: 8px; }
.thing { background: #fff; border-radius: 12px; padding: 10px 12px; border: 1px solid rgba(11,11,11,.07); }
.thing .head { display: flex; align-items: flex-start; gap: 8px; }
.thing .n { flex: none; width: 20px; height: 20px; border-radius: 50%; font-weight: 800; font-size: 10.5px;
            display: flex; align-items: center; justify-content: center; }
.thing .t { font-size: 10.5px; font-weight: 700; line-height: 1.25; padding-top: 2px; }
.thing .x { margin-top: 5px; font-size: 9px; color: #3a3936; line-height: 1.42; }
.signals { display: grid; align-items: stretch; background: #fffaf0; border: 1px solid #f1dfb8; border-radius: 12px; overflow: hidden; }
.signals .lead { padding: 8px 12px; background: #fdf0d3; font-size: 8px; font-weight: 700; letter-spacing: .12em;
                 text-transform: uppercase; color: #5c4400; display: flex; flex-direction: column; justify-content: center; gap: 2px; }
.signals .lead em { font-style: normal; font-weight: 600; letter-spacing: 0; text-transform: none; font-size: 8px; color: #6b5a2e; }
.signal { display: flex; align-items: center; gap: 7px; padding: 7px 10px; border-left: 1px solid #f1dfb8; }
.signal svg { flex: none; }
.signal b { display: block; font-size: 9.5px; color: #0b0b0b; }
.signal span { font-size: 9px; color: #3a3936; white-space: nowrap; }
.signal span svg { vertical-align: 0; margin: 0 1px; }
footer { margin-top: auto; padding: 7px 0 6mm; display: grid; grid-template-columns: 1fr auto; gap: 12px; align-items: end;
         font-size: 8px; color: #6b6a66; line-height: 1.5; border-top: 1px solid #dcdad3; }
footer b { color: #3a3936; }
.badge { display: inline-block; padding: 3px 8px; border-radius: 6px; background: #0d1433; color: #fff; font-weight: 700;
         font-size: 8px; letter-spacing: .1em; text-transform: uppercase; }
"""


CHART_W = 332                              # the chart card's inner width at A4 with 9 mm margins
CHART_HEIGHTS = (168, 152, 136, 120)       # tried in turn until the page fits
# then, as the last resort, the watch-list strip is left out (and the result says so)
LAYOUTS = [(h, True) for h in CHART_HEIGHTS] + [(CHART_HEIGHTS[-1], False)]


def build_html(fp: dict, nar: dict, *, edition_label: str | None = None, chart_h: int = CHART_HEIGHTS[0],
               with_signals: bool = True) -> str:
    ed = fp.get("edition") or {}
    banking, macro, cpi = ed.get("banking_period"), ed.get("macro_period"), ed.get("cpi_period")
    month_iso = banking or cpi or macro
    month = f"{MONTHS_LONG[_d(month_iso).month - 1]} {_d(month_iso).year}" if month_iso else ""
    checked = str(fp.get("as_of") or "")[:10]

    tile_html = []
    for t in _tiles(fp):
        c = THEMES[t["theme"]]
        unit = f'<small>{_esc(t["unit"])}</small>' if t["unit"] else ""
        mark = (_info(INK_2) if t["chip_icon"] == "info"
                else _arrow(t["dir"], {"good": GOOD, "bad": BAD}.get(t["tone"], INK_2)))
        chip = f'<span class="delta {t["tone"]}">{mark}{_esc(t["chip"])}</span>' if t["chip"] else ""
        tile_html.append(
            f'<div class="tile" data-c="{c["colour"]}" style="background:linear-gradient(180deg,{c["tint"]} 0,#fff 44px)">'
            f'<div class="th"><span class="ic">{_icon(t["theme"])}</span>{_esc(c["label"])}</div>'
            f'<div class="title">{_esc(t["title"])}</div><div class="sub">{_esc(t["sub"])}</div>'
            f'<div class="value">{_esc(t["value"])}{unit}</div><div class="period">{_esc(t["period"])}</div>{chip}'
            + (f'<div class="extra">{_info(INK_2) + " " if t.get("extra_icon") == "info" else ""}{_text(t["extra"])}</div>'
               if t["extra"] else "")
            + (t["bars"] or t["spark"]) + '</div>')

    # charts: series the deck already plots, last two years
    cpi_s = _series(fp, "ssc.cpi.all.yoy")[-24:]
    pv = policy_view(fp)
    s1 = [{"label": "Consumer price inflation, y/y", "short": "Inflation", "points": cpi_s,
           "colour": THEMES["prices"]["colour"]},
          {"label": "CBA refinancing rate", "short": "Policy rate", "points": _rate_points(pv, cpi_s),
           "colour": POLICY_INK, "step": True, "decimals": 2}]
    loans_s, dep_s = _series(fp, "cba.loans.total_ci.yoy")[-24:], _series(fp, "cba.deposits.total.yoy")[-24:]
    s2 = [{"label": "Loans to the economy, y/y", "short": "Loans", "points": loans_s, "colour": THEMES["lending"]["colour"]},
          {"label": "Total deposits, y/y", "short": "Deposits", "points": dep_s, "colour": THEMES["funding"]["colour"]}]
    chart1 = _line_chart(s1, w=CHART_W, h=chart_h, ticks_from=[p for p, _ in cpi_s])
    chart2 = _line_chart(s2, w=CHART_W, h=chart_h, ticks_from=[p for p, _ in loans_s])

    tile_ids = {"ssc.hl.gdp_nonoil.growth", "ssc.hl.gdp.growth", "ssc.cpi.all.yoy", "cba.policy.rate",
                "cba.loans.total_ci.yoy", "cba.deposits.total.yoy", "cba.deposits.fx_share", "cba.bank.npl.ratio",
                "cba.bank.pnl.net_profit"}
    thing_html = "".join(
        f'<div class="thing"><div class="head"><div class="n" style="background:{THEMES[t["theme"]]["colour"]};'
        f'color:{_on(THEMES[t["theme"]]["colour"])}">{i}</div><div class="t">{_esc(t["title"])}</div></div>'
        f'<div class="x">{_text(t["text"])}</div></div>'
        for i, t in enumerate(_takeaways(fp, nar, tile_ids), 1))

    signals = _signals(fp)[:3] if with_signals else []
    signal_html = ""
    if signals:
        to = (f'<svg width="14" height="8" viewBox="0 0 14 8" aria-hidden="true"><path d="M1 4h11M9 1l3 3-3 3" fill="none" '
              f'stroke="{INK_2}" stroke-width="1.3" stroke-linecap="round" stroke-linejoin="round"/></svg>')
        cells = "".join(f'<div class="signal">{_flag_icon()}<div><b>{_esc(s["name"])}</b><span>{_esc(s["from"])} {to} '
                        f'{_esc(s["to"])} in {s["months"]} months</span></div></div>' for s in signals)
        cols = "auto " + " ".join(["1fr"] * len(signals))
        signal_html = (f'<h2>Signals to check</h2><div class="signals" style="grid-template-columns:{cols}">'
                       f'<div class="lead">Watch list<em>a prompt to check,<br>not a risk score</em></div>{cells}</div>')

    nxt = _policy(fp).get("next_decision") or {}
    next_line = (f'Next CBA rate decision: <b>{_esc(_day(nxt["date"]))}</b>.' if nxt.get("date") and
                 str(nxt["date"])[:10] > checked else "")
    strip = []
    if banking:
        strip.append(("Banking data", f"end-{_month_short(banking)}"))
    if cpi:
        strip.append(("Prices", _month_short(cpi)))
    if macro:
        strip.append(("GDP", f"Jan–{MONTHS[_d(macro).month - 1]} {_d(macro).year}"))
    if pv["latest_decision"]:
        strip.append(("Latest CBA decision", _day(pv["latest_decision"])))
    if checked:
        strip.append(("Sources checked", _day(checked)))
    strip_html = "".join(f"<span><b>{_esc(k)}</b>{_esc(v)}</span>" for k, v in strip)
    blobs = "".join(
        f'<span class="blob" style="width:{s}px;height:{s}px;right:{r}px;top:{t}px;background:{c};opacity:{o}"></span>'
        for s, r, t, c, o in [(170, -54, -74, THEMES["economy"]["colour"], .5), (66, 104, 12, THEMES["prices"]["colour"], .92),
                              (34, 70, 74, THEMES["lending"]["colour"], .95), (48, 168, 58, THEMES["health"]["colour"], .88),
                              (22, 196, 18, "#eda100", .95), (84, -14, 40, THEMES["funding"]["colour"], .92)])
    accent_css = "".join(f'.tile[data-c="{c["colour"]}"]:before{{background:{c["colour"]}}}' for c in THEMES.values())
    companion = f"Companion to the {_esc(edition_label)}." if edition_label else "Companion to the Monthly Monitor."

    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<title>Azerbaijan Macro &amp; Banking Monitor — {_esc(month)} one-pager</title>
<style>{CSS}{accent_css}</style></head>
<body><div class="page">
<header>
  {blobs}
  <div class="kicker">Monthly one-pager · Internal</div>
  <h1>Azerbaijan Macro &amp; Banking Monitor</h1>
  <div class="month">{_esc(month)}</div>
  <div class="strip">{strip_html}</div>
</header>
<div class="headline">{_headline(fp)}</div>
<h2>The month in numbers</h2>
<div class="tiles">{''.join(tile_html)}</div>
<h2>Two-year trends</h2>
<div class="charts">
  <div class="card"><h3>Inflation and the policy rate</h3><div class="sub">% · the rate is the level set at each CBA meeting</div>
    {_legend(s1)}{chart1}</div>
  <div class="card"><h3>Lending and deposit growth</h3><div class="sub">% change on a year earlier · month-end stocks</div>
    {_legend(s2)}{chart2}</div>
</div>
<h2>Three things to know</h2>
<div class="things">{thing_html}</div>
{signal_html}
<footer>
  <div>Sources: Central Bank of the Republic of Azerbaijan (CBA) and the State Statistics Committee (SSC), as published.
  {companion} Every figure here is in the full report, where each one is traced to its source document.
  {('<br>' + next_line) if next_line else ''}</div>
  <div><span class="badge">Internal</span></div>
</footer>
</div></body></html>"""


# ------------------------------------------------------------------------------ printing

_MEASURE = """<script>
(function () {
  var page = document.querySelector('.page'), box = page.getBoundingClientRect(), style = getComputedStyle(page);
  var limit = box.bottom, right = box.right - parseFloat(style.paddingRight), worst = 0, clipped = [];
  page.querySelectorAll('*').forEach(function (el) {
    var r = el.getBoundingClientRect();
    if (r.height && r.bottom > limit + 0.5) worst = Math.max(worst, r.bottom - limit);
    // the header bleeds to the paper edge by design; everything else stays inside the margins
    if (r.width && !el.closest('header') && r.right > right + 0.5) clipped.push('wide:' + el.tagName + '.' + el.className + ':' + Math.ceil(r.right - right));
  });
  document.querySelectorAll('.tile, .thing, .card, .headline, .signal, .strip').forEach(function (el) {
    var dy = el.scrollHeight - el.clientHeight, dx = el.scrollWidth - el.clientWidth;
    if (dy > 1 || dx > 1) clipped.push(el.className + ':' + dx + 'x' + dy);
  });
  var footer = page.querySelector('footer'), last = footer.previousElementSibling;
  var slack = footer.getBoundingClientRect().top - last.getBoundingClientRect().bottom;
  document.body.setAttribute('data-overflow', String(Math.ceil(worst)));
  document.body.setAttribute('data-clipped', clipped.join('|'));
  document.body.setAttribute('data-slack', String(Math.floor(slack)));
})();
</script>"""


def _chromium() -> str | None:
    for cand in (os.environ.get("AZMONITOR_CHROMIUM"), "/opt/pw-browsers/chromium", shutil.which("google-chrome"),
                 shutil.which("chromium"), shutil.which("chromium-browser"), shutil.which("chrome")):
        if cand and Path(cand).exists():
            return cand
    return None


def _browser_args(browser: str, profile: str) -> list[str]:
    return [browser, "--headless=new", "--no-sandbox", "--disable-gpu", "--hide-scrollbars", f"--user-data-dir={profile}",
            "--run-all-compositor-stages-before-draw", "--virtual-time-budget=3000", "--window-size=794,1123"]


def measure(page_html: str, browser: str) -> dict[str, Any]:
    """Lay the page out in the browser that prints it; report overflow, clipping and spare height (px)."""
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        probe = Path(tmp) / "measure.html"
        probe.write_text(page_html.replace("</body>", _MEASURE + "</body>"), encoding="utf-8")
        run = subprocess.run(_browser_args(browser, str(Path(tmp) / "profile")) + ["--dump-dom", probe.as_uri()],
                             check=True, capture_output=True, text=True, timeout=120)
    attrs = dict(re.findall(r'data-(overflow|clipped|slack)="([^"]*)"', run.stdout))
    overflow, clipped = int(attrs.get("overflow") or 0), [c for c in (attrs.get("clipped") or "").split("|") if c]
    return {"overflow_px": overflow, "clipped": clipped, "slack_px": int(attrs.get("slack") or 0),
            "fits": overflow == 0 and not clipped}


def render_onepager(fp: dict, nar: dict, out_dir: Path, *, stem: str = "onepager",
                    edition_label: str | None = None) -> dict[str, Any]:
    """Write <stem>.html, and with a Chromium available <stem>.pdf (A4) and <stem>.png.

    The layout is measured first, in the browser that prints it. When a month's text runs long the
    charts are made shorter until the page fits, and as a last resort the watch-list strip is left
    out; a page that still would not fit is reported in `layout` with fits=false, so a caller can
    refuse to send it. The PNG is drawn from the PDF
    itself, so the two cannot differ."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    browser = _chromium()
    page_html, layout = build_html(fp, nar, edition_label=edition_label), None
    if browser:
        for chart_h, with_signals in LAYOUTS:
            page_html = build_html(fp, nar, edition_label=edition_label, chart_h=chart_h, with_signals=with_signals)
            layout = measure(page_html, browser) | {"chart_height": chart_h,
                                                    "signals_shown": with_signals and bool(_signals(fp))}
            if layout["fits"]:
                break
    page = out_dir / f"{stem}.html"
    page.write_text(page_html, encoding="utf-8")
    result: dict[str, Any] = {"html": str(page)}
    if not browser:
        result["error"] = "no Chromium found; set AZMONITOR_CHROMIUM"
        return result
    result["layout"] = layout
    pdf, png = out_dir / f"{stem}.pdf", out_dir / f"{stem}.png"
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as profile:
        subprocess.run(_browser_args(browser, profile) + ["--no-pdf-header-footer", f"--print-to-pdf={pdf}", page.as_uri()],
                       check=True, capture_output=True, timeout=120)
    result["pdf"] = str(pdf)
    result.update(_png_from_pdf(pdf, png))
    return result


def _png_from_pdf(pdf: Path, png: Path, dpi: int = 192) -> dict[str, Any]:
    """Rasterise the printed page (pdfium ships with pdfplumber, a dependency of the package)."""
    try:
        import pypdfium2 as pdfium
    except ImportError:
        return {"png_error": "pypdfium2 is not installed"}
    doc = pdfium.PdfDocument(str(pdf))
    try:
        if len(doc) != 1:
            return {"png_error": f"the PDF has {len(doc)} pages, not one"}
        doc[0].render(scale=dpi / 72).to_pil().save(png, optimize=True)
    finally:
        doc.close()
    return {"png": str(png)}


if __name__ == "__main__":  # python -m azmonitor.render.onepager fact_pack.json narrative.json out_dir [edition label]
    import sys
    fp_path, nar_path, out = sys.argv[1], sys.argv[2], sys.argv[3]
    print(json.dumps(render_onepager(json.loads(Path(fp_path).read_text()), json.loads(Path(nar_path).read_text()),
                                     Path(out), edition_label=sys.argv[4] if len(sys.argv) > 4 else None), indent=2))
