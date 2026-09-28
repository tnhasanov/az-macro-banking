"""Edition identity for CBA narrative publications.

The title on the download page is the only reliable identifier of an edition, and the same edition
appears with a different title in each language. `classify_title` maps either language to one
edition key so that the Azerbaijani original and the English translation join the same publication
and generate one release event.

Nothing here guesses a publication date: the title tells us which period a document is *about*,
not when it was released. Publication dates come from the page, the press release or the file.
"""
from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass

from ..util.periods import az_month_number, month_end

EN_MONTHS = {m.lower(): i for i, m in enumerate(
    ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"], start=1)}
EN_MONTH_ABBR = {m[:3].lower(): i for m, i in EN_MONTHS.items()}
MONTH_LABEL = {v: k.capitalize() for k, v in EN_MONTHS.items()}

ROMAN = {"i": 1, "ii": 2, "iii": 3, "iv": 4}
# a hyphen in these titles may be a hyphen, an en dash or an em dash
DASH = r"[-–—]"


@dataclass(frozen=True)
class EditionInfo:
    edition_key: str                       # sorts chronologically: 2026-08, 2025-H1, 2024-A, 2026-Y
    label_en: str                          # "August 2026", "H1 2025", "2024 (annual)", "for 2026"
    frequency: str                         # quarterly | semiannual | annual | event
    reporting_start: dt.date | None = None
    reporting_end: dt.date | None = None
    period_basis: str | None = None        # how the reporting period was established, or None when unknown
    forward_looking: bool = False          # a statement about the year ahead, not a report on a past period


def _month_from_any(token: str) -> int | None:
    t = token.strip().lower()
    return EN_MONTHS.get(t) or EN_MONTH_ABBR.get(t[:3]) or az_month_number(token)


def _mpr(title: str) -> EditionInfo | None:
    # current naming, both languages: "Pul Siyasəti İcmalı – Avqust 2026" / "Monetary policy review – August 2026"
    m = re.search(rf"(?:icmal[ıi]|review)\s*{DASH}\s*([A-Za-zƏəĞğİıÖöŞşÜüÇç]+)\s+(\d{{4}})", title, re.IGNORECASE)
    if m:
        mon = _month_from_any(m.group(1))
        if mon:
            year = int(m.group(2))
            return EditionInfo(f"{year:04d}-{mon:02d}", f"{MONTH_LABEL[mon]} {year}", "quarterly",
                               period_basis=None)
    # legacy naming: "2017-ci ilin yanvar-dekabr ayları üzrə Pul Siyasəti icmalı" / "Monetary policy review, January - December 2017"
    m = re.search(r"(\d{4})-c[iıuü] il[ni]+\s+yanvar[-–—]\s*([A-Za-zƏəĞğİıÖöŞşÜüÇç]+)\s+ay", title, re.IGNORECASE)
    if m:
        year, mon = int(m.group(1)), _month_from_any(m.group(2))
        if mon:
            return EditionInfo(f"{year:04d}-{mon:02d}", f"January–{MONTH_LABEL[mon]} {year}", "quarterly",
                               dt.date(year, 1, 1), month_end(year, mon), "title states the reporting window")
    m = re.search(rf"January\s*{DASH}\s*([A-Za-z]+)\s+(\d{{4}})", title, re.IGNORECASE)
    if m:
        mon, year = _month_from_any(m.group(1)), int(m.group(2))
        if mon is None and m.group(1).lower().startswith("dece"):   # the site has a "Deceember" typo
            mon = 12
        if mon:
            return EditionInfo(f"{year:04d}-{mon:02d}", f"January–{MONTH_LABEL[mon]} {year}", "quarterly",
                               dt.date(year, 1, 1), month_end(year, mon), "title states the reporting window")
    return None


def _fsr(title: str) -> EditionInfo | None:
    # half-year: "2025-ci ilin I yarımili üzrə ..." / "... for Half 1, 2025" / "... for Half I, 2022"
    m = re.search(r"(\d{4})-c[iıuü] il[ni]+\s+(I{1,3}|IV)\s+yar[ıi]m[ıi]l", title, re.IGNORECASE)
    if m:
        year, half = int(m.group(1)), ROMAN.get(m.group(2).lower(), 1)
        return _half(year, half)
    m = re.search(r"Half\s*(1|2|I{1,2})\s*,?\s*(\d{4})", title, re.IGNORECASE)
    if m:
        h = m.group(1).lower()
        half = 2 if h in ("2", "ii") else 1
        return _half(int(m.group(2)), half)
    # annual: "Maliyyə sabitliyi hesabatı - 2024" / "Financial Stability Report for 2024"
    m = re.search(r"(?:for|[-–—])\s*(\d{4})\s*$", title.strip(), re.IGNORECASE) or re.search(r"(\d{4})", title)
    if m:
        year = int(m.group(1))
        return EditionInfo(f"{year:04d}-A", f"{year} (annual)", "annual", dt.date(year, 1, 1), dt.date(year, 12, 31),
                           "title states the reporting year")
    return None


def _half(year: int, half: int) -> EditionInfo:
    start = dt.date(year, 1, 1) if half == 1 else dt.date(year, 7, 1)
    end = dt.date(year, 6, 30) if half == 1 else dt.date(year, 12, 31)
    return EditionInfo(f"{year:04d}-H{half}", f"H{half} {year}", "semiannual", start, end, "title states the reporting half-year")


def _directions(title: str) -> EditionInfo | None:
    """Annual statement on the directions of monetary policy *for* the coming year."""
    m = re.search(r"(\d{4})-c[iıuü] il\s*(?:və ortamüddətli dövr\s*)?üçün", title, re.IGNORECASE) \
        or re.search(r"for\s+(\d{4})(?:\s+and the medium term)?", title, re.IGNORECASE) \
        or re.search(r"(\d{4})", title)
    if not m:
        return None
    year = int(m.group(1))
    return EditionInfo(f"{year:04d}-Y", f"for {year}", "annual", dt.date(year, 1, 1), dt.date(year, 12, 31),
                       "title states the policy year", forward_looking=True)


def classify_title(pub_type: str, title: str) -> EditionInfo | None:
    if pub_type == "monetary_policy_review":
        return _mpr(title)
    if pub_type == "financial_stability_report":
        return _fsr(title)
    if pub_type == "policy_directions":
        return _directions(title)
    return None


def decision_edition(announced: dt.date) -> EditionInfo:
    return EditionInfo(announced.isoformat(), announced.strftime("%d %B %Y"), "event", announced, announced,
                       "announcement date")


def publication_id(pub_type: str, key: str) -> str:
    return f"{pub_type}:{key}"
