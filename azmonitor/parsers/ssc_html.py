"""Parser for the SSC headline table published as HTML on each monthly edition page.

Two layouts exist:
  * current (editions from about November 2025): <table id="macro_iq">, growth columns as "+1,2%"
  * legacy  (earlier editions): <table id="tmacro">, growth as an index "101,3" (= +1.3%),
    "*" markers meaning a one-month-shorter YTD window (explained in the page note),
    stock rows carrying "01 <month> vəziyyətinə" dates, CPI as the y/y rate of the headline month.
Both feed the same series ids (ssc.hl.<key>.level / .growth) with explicit period types.
"""
from __future__ import annotations

import datetime as dt
import re
from pathlib import Path
from typing import Any

from bs4 import BeautifulSoup

from ..util.numbers import parse_number
from ..util.periods import az_month_number, month_end, shift_months
from .base import Observation, ParseResult, ParserError
from .ssc_headline import HEADLINE_ROWS, SECTION_PATTERNS, normalise_label, period_from_header, period_from_label

NOMINAL_KEYS = {"budget_revenue", "budget_expenditure", "strategic_reserves", "external_public_debt", "credit", "hh_deposits", "income",
                "wage", "trade_turnover", "exports", "exports_nonoil", "imports", "overdue"}

LEGACY_ROWS = [
    # key, pattern, kind, unit
    ("gdp", r"^Ümumi daxili məhsul, milyon manat", "ytd_flow", "AZN mln"),
    ("gdp_nonoil", r"^o cümlədən qeyri[- ]neft-qaz ÜDM", "ytd_flow", "AZN mln"),
    ("industry", r"^Sənaye məhsulu, milyon manat", "ytd_flow", "AZN mln"),
    ("industry_nonoil", r"^o cümlədən qeyri[- ]neft-qaz sənayesi", "ytd_flow", "AZN mln"),
    ("investment", r"^Əsas kapitala yönəldilmiş vəsaitlər", "ytd_flow", "AZN mln"),
    ("investment_nonoil", r"^o cümlədən qeyri[- ]neft-qaz sektoruna", "ytd_flow", "AZN mln"),
    ("agriculture", r"^Kənd təsərrüfatı məhsulu", "ytd_flow", "AZN mln"),
    ("ict", r"^İnformasiya və rabitə xidmətləri", "ytd_flow", "AZN mln"),
    ("retail", r"^Pərakəndə ticarət dövriyyəsi", "ytd_flow", "AZN mln"),
    ("budget_revenue", r"^Dövlət büdcəsinin gəlirləri", "ytd_flow", "AZN mln"),
    ("budget_expenditure", r"^Dövlət büdcəsinin xərcləri", "ytd_flow", "AZN mln"),
    ("budget_balance", r"^Dövlət büdcəsinin (profisiti|kəsiri)", "ytd_flow", "AZN mln"),
    ("income", r"^Əhalinin nominal gəlirləri", "ytd_flow", "AZN mln"),
    ("hh_deposits", r"^Əhalinin banklardakı əmanətləri", "stock", "AZN mln"),
    ("credit", r"^Kredit qoyuluşları", "stock", "AZN mln"),
    ("overdue", r"^o cümlədən vaxtı keçmiş kreditlər", "stock", "AZN mln"),
    ("wage", r"^Orta aylıq nominal əməkhaqqı", "ytd_average", "AZN per month"),
    ("cpi_yoy_month", r"^İstehlak qiymətlərinin indeksi", "growth_only", "%"),
]
LEGACY_TRADE_HEADS = [("trade_turnover", r"^Xarici ticarət dövriyyəsi"), ("exports", r"^o cümlədən: ixrac"), ("exports_nonoil", r"^ondan qeyri[- ]neft-qaz ixracı"), ("imports", r"^idxal")]

_LEGACY_STOCK_RE = re.compile(r"(\d{4})-c[iıuü] il 0?(\d{1,2}) ([a-zəığöşüçİ]+) vəziyyətinə", re.IGNORECASE)


def _mk(res: ParseResult, key: str, kind: str, unit: str, level, growth, pend: dt.date, source_id: str, ref: str, label: str, basis: str | None,
        method: str = "html_table", growth_note: str | None = None) -> None:
    pstart = dt.date(pend.year, 1, 1)
    ptype_level = {"ytd_flow": "ytd_flow", "ytd_average": "ytd_average", "stock": "month_end_stock", "growth_only": "ytd_growth_yoy"}[kind]
    if kind != "growth_only" and level is not None:
        res.observations.append(Observation(
            series_id=f"ssc.hl.{key}.level", period_end=pend, period_start=None if kind == "stock" else pstart, value=level.value, value_raw=level.raw,
            freq="M", period_type=ptype_level, missing_reason=level.missing_reason, unit=unit, source_id=source_id, cell_ref=ref + ":level",
            label_original=label, extraction_method=method, flags=level.flags, basis=basis,
        ))
    if key == "cpi_yoy_month":
        gtype = "monthly_growth_yoy"
    else:
        gtype = "stock_growth_yoy" if kind == "stock" else "ytd_growth_yoy"
    g_basis = growth_note or ("nominal growth" if key in NOMINAL_KEYS else "real growth")
    res.observations.append(Observation(
        series_id=f"ssc.hl.{key}.growth", period_end=pend, period_start=None if kind == "stock" else pstart, value=growth.value, value_raw=growth.raw,
        freq="M", period_type=gtype, missing_reason=growth.missing_reason, unit="%", source_id=source_id, cell_ref=ref + ":growth",
        label_original=label, extraction_method=method, flags=growth.flags, basis=((basis + "; ") if basis else "") + g_basis,
    ))


def _growth_from_index(cell: str):
    """Legacy layout: growth column is an index in % of the base (101,3). Strip * markers."""
    stars = cell.count("*")
    p = parse_number(cell.replace("*", "").strip())
    if p.value is not None and not p.is_percent and p.missing_reason is None:
        p.value = p.value - 100.0
        p.flags = p.flags + ["index_converted_to_growth"]
    if stars:
        p.flags = p.flags + [f"marker:{'*' * stars}"]
    return p, stars


def _level_with_stars(cell: str):
    stars = cell.count("*")
    p = parse_number(cell.replace("*", "").strip())
    if stars:
        p.flags = p.flags + [f"marker:{'*' * stars}"]
    return p, stars


def parse_current_layout(table, res: ParseResult, source_id: str) -> int:
    trs = table.select("tr")
    header_txt = " ".join(trs[0].get_text(" ", strip=True).split())
    head_period = period_from_header(header_txt)
    if not head_period:
        raise ParserError(f"cannot determine headline period from header: {header_txt[:100]}")
    section_override: dt.date | None = None
    n = 0
    for i, tr in enumerate(trs[1:], start=2):
        cells = [" ".join(c.get_text(" ", strip=True).split()) for c in tr.select("th,td")]
        if cells and re.fullmatch(r"\d", cells[0]):
            cells = cells[1:]
        if not cells:
            continue
        label = normalise_label(cells[0])
        for sec, pat in SECTION_PATTERNS.items():
            if re.search(pat, label):
                ov = period_from_label(label)
                section_override = ov[1] if ov else None
        if len(cells) < 3:
            continue
        for row in HEADLINE_ROWS:
            if not re.search(row.pattern, label):
                continue
            level, growth = parse_number(cells[1]), parse_number(cells[2])
            ov = period_from_label(label)
            basis = None
            if ov:
                pend, basis = ov[1], f"row-specific period note: {label[label.find('('):]}"
                if row.section:  # a note on the first row of a section applies to the rows that follow it
                    section_override = ov[1]
            elif row.section and section_override:
                pend, basis = section_override, "section-level period note applied"
            else:
                pend = head_period
            _mk(res, row.key, row.kind, row.unit, level, growth, pend, source_id, f"macro_iq!r{i}", label, basis)
            n += 1
            break
    res.meta["headline_period_end"] = head_period.isoformat()
    res.meta["layout"] = "current"
    return n


def parse_legacy_layout(table, note_text: str, res: ParseResult, source_id: str) -> int:
    trs = table.select("tr")
    header_txt = " ".join(trs[0].get_text(" ", strip=True).split())
    head_period = period_from_header(header_txt)
    if not head_period:
        raise ParserError(f"legacy layout: cannot determine headline period from header: {header_txt[:100]}")
    # "*" marker window from the note, default one month shorter
    star_period = shift_months(head_period, -1)
    m = re.search(r"\*\s*(\d{4})-c[iıuü] ilin yanvar-([a-zəığöşüçİ]+) ayları", note_text)
    if m:
        mon = az_month_number(m.group(2))
        if mon:
            star_period = month_end(int(m.group(1)), mon)
    n = 0
    trade_key: str | None = None
    for i, tr in enumerate(trs[1:], start=2):
        cells = [" ".join(c.get_text(" ", strip=True).split()) for c in tr.select("th,td")]
        if not cells:
            continue
        label = normalise_label(cells[0])
        for key, pat in LEGACY_TRADE_HEADS:
            if re.search(pat, label):
                trade_key = key
        if trade_key and label.startswith("faktiki qiymətlərlə") and len(cells) >= 3:
            level, s1 = _level_with_stars(cells[1])
            growth, s2 = _growth_from_index(cells[2])
            pend = star_period if (s1 or s2) else head_period
            _mk(res, trade_key, "ytd_flow", "USD mln", level, growth, pend, source_id, f"tmacro!r{i}", label,
                "legacy layout; '*' marker = shorter YTD window per page note" if (s1 or s2) else "legacy layout")
            n += 1
            trade_key = None if trade_key == "imports" else trade_key
            continue
        if len(cells) < 3:
            continue
        for key, pat, kind, unit in LEGACY_ROWS:
            if not re.search(pat, label):
                continue
            level, s1 = _level_with_stars(cells[1])
            growth, s2 = _growth_from_index(cells[2])
            basis = "legacy layout"
            if kind == "stock":
                sm = _LEGACY_STOCK_RE.search(label)
                if sm:
                    mon = az_month_number(sm.group(3))
                    d = dt.date(int(sm.group(1)), mon, int(sm.group(2))) if mon else None
                    pend = (d - dt.timedelta(days=1)) if d and d.day == 1 else (d or head_period)
                    basis += f"; stock date from label {sm.group(0)}"
                else:
                    pend = head_period
            elif key == "cpi_yoy_month":
                pend = head_period
                basis += "; CPI y/y for the headline month (page note 1)"
            elif s1 or s2:
                pend = star_period
                basis += "; '*' marker = shorter YTD window per page note"
            else:
                pend = head_period
            _mk(res, key, kind, unit, level if kind != "growth_only" else None, growth, pend, source_id, f"tmacro!r{i}", label, basis)
            n += 1
            break
    res.meta["headline_period_end"] = head_period.isoformat()
    res.meta["layout"] = "legacy"
    return n


def parse_html_text(html: str, *, source_id: str, page_url: str = "") -> ParseResult:
    res = ParseResult()
    soup = BeautifulSoup(html, "html.parser")
    table = soup.select_one("#macro_iq")
    if table is not None:
        n = parse_current_layout(table, res, source_id)
    else:
        table = soup.select_one("#tmacro") or next((t for t in soup.select("table") if "Ümumi daxili məhsul" in t.get_text()), None)
        if table is None:
            raise ParserError("SSC headline table not found on page")
        notes = " ".join(" ".join(el.get_text(" ", strip=True).split()) for el in soup.select(".single-news p, .single-news .note, .note"))
        n = parse_legacy_layout(table, notes, res, source_id)
    meta = soup.select_one(".news-meta")
    if meta:
        res.meta["page_date_text"] = meta.get_text(" ", strip=True)
    res.meta.update({"rows_matched": n, "page_url": page_url})
    if n < 10:
        res.errors.append(f"only {n} headline rows matched")
    return res


def parse(path: Path, spec: dict[str, Any], *, source_id: str, dataset_id: str, context: dict[str, Any] | None = None) -> ParseResult:
    html = Path(path).read_text(encoding="utf-8", errors="replace")
    return parse_html_text(html, source_id=source_id, page_url=(context or {}).get("url", ""))
