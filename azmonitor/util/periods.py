"""Reference-period helpers: month-ends, Azerbaijani month names, Baku time."""
from __future__ import annotations

import calendar
import datetime as dt
import re
from zoneinfo import ZoneInfo

BAKU = ZoneInfo("Asia/Baku")

AZ_MONTHS = {
    "yanvar": 1, "fevral": 2, "mart": 3, "aprel": 4, "may": 5, "iyun": 6, "iyul": 7,
    "avqust": 8, "sentyabr": 9, "oktyabr": 10, "noyabr": 11, "dekabr": 12,
}
AZ_MONTHS_ASCII = {"iyun": 6, "iyul": 7}
EN_MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"]
AZ_MONTHS_TITLE = ["Yanvar", "Fevral", "Mart", "Aprel", "May", "İyun", "İyul", "Avqust", "Sentyabr", "Oktyabr", "Noyabr", "Dekabr"]


def az_lower(text: str) -> str:
    """Lower-case Azerbaijani text without the dotted-I combining artefact ('İ'.lower() -> 'i̇')."""
    return text.replace("İ", "i").replace("I", "ı").lower()


def now_baku() -> dt.datetime:
    return dt.datetime.now(tz=BAKU)


def today_baku() -> dt.date:
    return now_baku().date()


def month_end(year: int, month: int) -> dt.date:
    return dt.date(year, month, calendar.monthrange(year, month)[1])


def month_start(year: int, month: int) -> dt.date:
    return dt.date(year, month, 1)


def quarter_end(year: int, q: int) -> dt.date:
    return month_end(year, q * 3)


def prev_month_end(d: dt.date) -> dt.date:
    first = d.replace(day=1)
    return first - dt.timedelta(days=1)


def shift_months(d: dt.date, months: int) -> dt.date:
    """Shift a month-end date by n months, returning the month-end of the target month."""
    y, m = d.year, d.month + months
    y += (m - 1) // 12
    m = (m - 1) % 12 + 1
    return month_end(y, m)


def az_month_number(name: str) -> int | None:
    key = name.strip().lower().replace("i̇", "i")
    key = key.replace("İ", "i").replace("ı", "i")
    for k, v in AZ_MONTHS.items():
        if key.startswith(k.replace("ı", "i")):
            return v
    return None


def parse_ddmmyyyy(text: str) -> dt.date | None:
    m = re.search(r"(\d{1,2})[./](\d{1,2})[./](\d{4})", text)
    if not m:
        return None
    d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
    try:
        return dt.date(y, mo, d)
    except ValueError:
        return None


def parse_iso_date(text: str) -> dt.date | None:
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})", text)
    if not m:
        return None
    try:
        return dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None


def parse_as_of(text: str | None) -> dt.date:
    """Explicit as-of definition: a calendar date interpreted as an information cutoff
    at 23:59 Asia/Baku. Defaults to today in Baku."""
    if not text:
        return today_baku()
    d = parse_iso_date(text) or parse_ddmmyyyy(text)
    if not d:
        raise ValueError(f"Unrecognised as-of date: {text!r} (use YYYY-MM-DD)")
    return d


def period_label(period_end: dt.date, period_type: str, lang: str = "en", period_start: dt.date | None = None) -> str:
    months = EN_MONTHS if lang == "en" else AZ_MONTHS_TITLE
    mname = months[period_end.month - 1]
    if period_type in ("ytd_flow", "ytd_average", "ytd_growth", "ytd_average_rate"):
        if period_end.month == 1:
            return f"{mname} {period_end.year}"
        first = months[0]
        return f"{first[:3]}–{mname[:3]} {period_end.year}"
    if period_type in ("month_end_stock", "month_end_average_rate"):
        return f"end-{mname[:3]} {period_end.year}" if lang == "en" else f"{mname} sonu {period_end.year}"
    if period_type.startswith("quarter"):
        q = (period_end.month - 1) // 3 + 1
        return f"Q{q} {period_end.year}"
    if period_type.startswith("annual"):
        return f"{period_end.year}"
    return f"{mname[:3]} {period_end.year}"


def ym(d: dt.date) -> str:
    return f"{d.year:04d}-{d.month:02d}"
