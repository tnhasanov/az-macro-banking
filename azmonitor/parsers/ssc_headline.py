"""Shared definitions for the SSC headline table "Əsas makroiqtisadi göstəricilərin icmalı".

The same table is published as an HTML table on the SSC macroeconomy news page and as
Table 1 of the monthly PDF report. Row labels carry their own reference-period notes
(e.g. "(01.08.2026-cı il vəziyyətinə)" for stocks, "(2026-cı ilin yanvar-iyul ayları üzrə)"
for a shorter YTD window) which override the page headline period. Section headings
can carry a note that applies to the rows beneath them (foreign trade).
"""
from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass

from ..util.periods import az_month_number, month_end, parse_ddmmyyyy, prev_month_end


@dataclass(frozen=True)
class HeadlineRow:
    key: str
    pattern: str            # regex on the normalised label
    kind: str               # ytd_flow | ytd_average | stock | growth_only
    unit: str
    label_en: str
    source_note: str = ""
    section: str | None = None


HEADLINE_ROWS: list[HeadlineRow] = [
    HeadlineRow("gdp", r"^Ümumi daxili məhsul \(ÜDM\)", "ytd_flow", "AZN mln", "GDP, nominal (YTD) and real growth"),
    HeadlineRow("gdp_oil", r"^neft-qaz ÜDM", "ytd_flow", "AZN mln", "Oil-gas GDP, nominal (YTD) and real growth"),
    HeadlineRow("gdp_nonoil", r"^qeyri[- ]neft-qaz ÜDM", "ytd_flow", "AZN mln", "Non-oil-gas GDP, nominal (YTD) and real growth"),
    HeadlineRow("industry", r"^Sənaye məhsulu", "ytd_flow", "AZN mln", "Industrial output (YTD) and real growth"),
    HeadlineRow("industry_nonoil", r"^qeyri[- ]neft-qaz sənayesi", "ytd_flow", "AZN mln", "Non-oil-gas industrial output (YTD)"),
    HeadlineRow("agriculture", r"^Kənd təsərrüfatı məhsulu", "ytd_flow", "AZN mln", "Agricultural output (YTD) and real growth"),
    HeadlineRow("transport", r"^Nəqliyyat və anbar", "ytd_flow", "AZN mln", "Transport and storage services (YTD)"),
    HeadlineRow("ict", r"^İnformasiya və rabitə", "ytd_flow", "AZN mln", "Information and communication services (YTD)"),
    HeadlineRow("retail", r"^Pərakəndə ticarət dövriyyəsi", "ytd_flow", "AZN mln", "Retail trade turnover (YTD) and real growth"),
    HeadlineRow("investment", r"^Əsas kapitala investisiyalar", "ytd_flow", "AZN mln", "Fixed capital investment (YTD) and real growth"),
    HeadlineRow("investment_nonoil", r"^qeyri[- ]neft-qaz sektoru", "ytd_flow", "AZN mln", "Non-oil-gas fixed capital investment (YTD)"),
    HeadlineRow("budget_revenue", r"^Dövlət büdcəsinin gəlirləri", "ytd_flow", "AZN mln", "State budget revenue (YTD), nominal growth"),
    HeadlineRow("budget_expenditure", r"^Dövlət büdcəsinin xərcləri", "ytd_flow", "AZN mln", "State budget expenditure (YTD), nominal growth"),
    HeadlineRow("budget_balance", r"^Büdcə (artıqlığı|kəsiri)", "ytd_flow", "AZN mln", "State budget balance (YTD)"),
    HeadlineRow("strategic_reserves", r"^Strateji valyuta ehtiyatları", "stock", "USD mln", "Strategic foreign-exchange reserves (SSC headline; not CBA reserves alone)"),
    HeadlineRow("external_public_debt", r"^Xarici dövlət borcu", "stock", "USD mln", "External public debt"),
    HeadlineRow("credit", r"^İqtisadiyyata kredit qoyuluşları", "stock", "AZN mln", "Loans to the economy (SSC republication of CBA data)"),
    HeadlineRow("hh_deposits", r"^Fiziki şəxslərin banklardakı əmanətləri", "stock", "AZN mln", "Household bank deposits (SSC republication of CBA data)"),
    HeadlineRow("income", r"^Əhalinin nominal gəlirləri", "ytd_flow", "AZN mln", "Nominal money income of the population (YTD)"),
    HeadlineRow("wage", r"^Orta aylıq nominal əməkhaqqı", "ytd_average", "AZN per month", "Average monthly nominal wage (YTD average)"),
    HeadlineRow("cpi", r"^İnflyasiya", "growth_only", "%", "CPI inflation, YTD average vs same period of previous year"),
    HeadlineRow("trade_turnover", r"^Ticarət dövriyyəsinin həcmi", "ytd_flow", "USD mln", "Foreign trade turnover (YTD)", section="trade"),
    HeadlineRow("exports", r"^İxrac$|^İxrac\b", "ytd_flow", "USD mln", "Exports (YTD)", section="trade"),
    HeadlineRow("exports_nonoil", r"^qeyri[- ]neft-qaz ixracı", "ytd_flow", "USD mln", "Non-oil-gas exports (YTD)", section="trade"),
    HeadlineRow("imports", r"^İdxal", "ytd_flow", "USD mln", "Imports (YTD)", section="trade"),
]

SECTION_PATTERNS = {"trade": r"^Xarici ticarət"}

_STOCK_RE = re.compile(r"\(\s*(\d{2}\.\d{2}\.\d{4})[^)]*vəziyyətinə\s*\)")
_YTD_RE = re.compile(r"\(\s*(\d{4})-c[iıuü] ilin yanvar\s*-\s*([a-zəığöşüçİ]+)\s+ay(?:ları|ı)\s+üzrə\s*\)", re.IGNORECASE)
_SINGLE_MONTH_RE = re.compile(r"\(\s*(\d{4})-c[iıuü] ilin ([a-zəığöşüçİ]+) ayı üzrə\s*\)", re.IGNORECASE)
_HEADER_YTD_RE = re.compile(r"(\d{4})-c[iıuü] ilin yanvar\s*-\s*([a-zəığöşüçİ]+)\s+ay", re.IGNORECASE)
_HEADER_MONTH_RE = re.compile(r"(\d{4})-c[iıuü] ilin ([a-zəığöşüçİ]+) ay", re.IGNORECASE)


def normalise_label(text: str) -> str:
    t = " ".join(text.replace("\xa0", " ").split())
    t = re.sub(r"\s\d\s?,", ",", t)          # footnote digits "əməkhaqqı 2 , manat"
    t = re.sub(r"(\S)\d\s,", r"\1,", t)
    t = re.sub(r"ları\s?\d\b", "ları", t)      # "qoyuluşları1"
    t = re.sub(r"(?<=[^\W\d_])\d(?=[\s,]|$)", "", t)   # trailing footnote digit glued to a word ("həcmi2")
    return t.strip()


def period_from_label(label: str) -> tuple[str, dt.date] | None:
    """Return ('stock', date) or ('ytd', month_end) when the label carries an explicit period note."""
    m = _STOCK_RE.search(label)
    if m:
        d = parse_ddmmyyyy(m.group(1))
        if d:
            return ("stock", prev_month_end(d) if d.day == 1 else d)
    m = _YTD_RE.search(label)
    if m:
        mon = az_month_number(m.group(2))
        if mon:
            return ("ytd", month_end(int(m.group(1)), mon))
    m = _SINGLE_MONTH_RE.search(label)
    if m:  # "(2026-cı ilin yanvar ayı üzrə)" = January only
        mon = az_month_number(m.group(2))
        if mon:
            return ("ytd", month_end(int(m.group(1)), mon))
    return None


def period_from_header(text: str) -> dt.date | None:
    m = _HEADER_YTD_RE.search(text)
    if m:
        mon = az_month_number(m.group(2))
        if mon:
            return month_end(int(m.group(1)), mon)
    m = _HEADER_MONTH_RE.search(text)
    if m:
        mon = az_month_number(m.group(2))
        if mon:
            return month_end(int(m.group(1)), mon)
    m = re.search(r"(\d{4})-c[iıuü] il\b", text)
    if m:  # full-year edition ("2025-ci il, faktiki") -> January-December
        return month_end(int(m.group(1)), 12)
    return None
