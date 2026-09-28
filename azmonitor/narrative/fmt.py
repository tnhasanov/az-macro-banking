"""Number and period formatting used by the narrative and the renderer (presentation rounding only)."""
from __future__ import annotations

import datetime as dt

from ..util.periods import EN_MONTHS, AZ_MONTHS_TITLE


def num(v: float | None, dec: int = 1, unit: str | None = None, sign: bool = False, lang: str = "en") -> str:
    if v is None:
        return "n/a"
    if unit in ("AZN mln", "USD mln", "AZN per month") and abs(v) >= 100:
        dec = 0 if abs(v) >= 1000 else 1
    s = f"{v:+,.{dec}f}" if sign else f"{v:,.{dec}f}"
    if unit == "%":
        return s + "%"
    if unit == "pp":
        return s + " pp"
    return s


def money(v: float | None, unit: str = "AZN mln") -> str:
    if v is None:
        return "n/a"
    cur = "AZN" if unit.startswith("AZN") else ("USD" if unit.startswith("USD") else "")
    if abs(v) >= 1000:
        return f"{cur} {v/1000:,.1f} bn".strip()
    return f"{cur} {v:,.0f} mln".strip()


def plabel(period_iso: str | None, period_type: str | None, lang: str = "en") -> str:
    if not period_iso:
        return "n/a"
    d = dt.date.fromisoformat(period_iso)
    months = EN_MONTHS if lang == "en" else AZ_MONTHS_TITLE
    m = months[d.month - 1][:3]
    pt = period_type or ""
    if pt.startswith("ytd") or "ytd" in pt:
        return f"Jan–{m} {d.year}" if d.month > 1 else f"Jan {d.year}"
    if pt.startswith("month_end") or pt.startswith("stock"):
        return f"end-{m} {d.year}"
    if pt.startswith("quarter"):
        return f"Q{(d.month - 1)//3 + 1} {d.year}"
    if pt.startswith("annual"):
        return str(d.year)
    return f"{m} {d.year}"


def change_word(v: float | None, up: str = "up", down: str = "down", flat: str = "unchanged", eps: float = 0.05) -> str:
    if v is None:
        return "n/a"
    if abs(v) < eps:
        return flat
    return up if v > 0 else down
