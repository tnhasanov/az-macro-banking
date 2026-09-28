"""Parser for CBA monetary tables laid out as year header rows followed by month rows.

Layout (e.g. Table 2.6):
    r6  Tarix | header ...
    ...
    2005          <- year row: annual value equals the December value
    1 .. 12       <- month rows (int or zero-padded strings '01'..'12')
Month rows produce month-end observations. Year rows are used only for a
consistency check (annual == December) and are not stored as observations.
"""
from __future__ import annotations

import datetime as dt
import re
from pathlib import Path
from typing import Any

from ..util.numbers import parse_number
from ..util.periods import month_end
from .base import Observation, ParseResult, ParserError
from .xl import cell_ref, find_sheet, load_sheets


def _as_year(v: Any) -> int | None:
    if isinstance(v, (int, float)) and 1990 <= int(v) <= 2100 and float(v).is_integer():
        return int(v)
    if isinstance(v, str) and re.fullmatch(r"\s*(19|20)\d{2}\s*", v):
        return int(v.strip())
    if isinstance(v, dt.datetime) and v.month == 12 and v.day == 31:
        return None
    return None


def _as_month(v: Any) -> int | None:
    if isinstance(v, (int, float)) and float(v).is_integer() and 1 <= int(v) <= 12:
        return int(v)
    if isinstance(v, str) and re.fullmatch(r"\s*(0?[1-9]|1[0-2])\s*", v):
        return int(v.strip())
    if isinstance(v, dt.datetime):
        return v.month
    return None


def check_header(rows: list[list[Any]], spec: dict[str, Any], sheet: str) -> None:
    hc = spec.get("header_check")
    if not hc:
        return
    r, c, needle = int(hc["row"]) - 1, int(hc["col"]), hc["contains"]
    try:
        val = rows[r][c]
    except IndexError:
        raise ParserError(f"{sheet}: header check cell r{r+1}c{c} missing")
    if needle.lower() not in str(val or "").lower():
        raise ParserError(f"{sheet}: header check failed at r{r+1}c{c}: expected {needle!r}, found {val!r}")


def parse(path: Path, spec: dict[str, Any], *, source_id: str, dataset_id: str, context: dict[str, Any] | None = None) -> ParseResult:
    res = ParseResult()
    sheets = load_sheets(path)
    sheet_name, rows = find_sheet(sheets, spec["sheet"])
    check_header(rows, spec, sheet_name)
    date_col = int(spec.get("date_col", 0))
    unit = spec.get("unit")
    ptype = spec.get("period_type", "month_end_stock")
    series = spec["series"]
    hdr_row = int(spec.get("header_check", {}).get("row", 6))
    year: int | None = None
    annual_values: dict[tuple[str, int], float | None] = {}
    dec_values: dict[tuple[str, int], float | None] = {}
    n_year_rows = 0
    for i in range(hdr_row, len(rows)):
        row = rows[i]
        if date_col >= len(row):
            continue
        key = row[date_col]
        y = _as_year(key)
        if y is not None:
            year = y
            n_year_rows += 1
            for s in series:
                col = int(s["col"])
                if col < len(row):
                    annual_values[(s["id"], y)] = parse_number(row[col]).value
            continue
        m = _as_month(key)
        if m is None or year is None:
            continue
        pend = month_end(year, m)
        for s in series:
            col = int(s["col"])
            if col >= len(row):
                continue
            p = parse_number(row[col])
            scale = float(s.get("scale", spec.get("scale", 1.0)))
            val = p.value * scale if p.value is not None else None
            if ptype == "monthly_flow":
                pstart = pend.replace(day=1)
            elif ptype in ("monthly_average_rate",):
                pstart = pend.replace(day=1)
            else:
                pstart = None
            res.observations.append(Observation(
                series_id=s["id"], period_end=pend, period_start=pstart, value=val, value_raw=p.raw, freq="M", period_type=ptype,
                dims=dict(s.get("dims", {})), missing_reason=p.missing_reason, unit=s.get("unit", unit),
                scale_note=(f"x{scale}" if scale != 1.0 else None), currency=s.get("currency"),
                population=s.get("population", spec.get("population")), source_id=source_id, sheet=sheet_name,
                cell_ref=cell_ref(sheet_name, i, col), label_original=s.get("label_az"), extraction_method="xlsx_cell",
                flags=p.flags,
            ))
            if m == 12:
                dec_values[(s["id"], year)] = val
    # consistency: annual row equals December value where both exist
    mism = 0
    for k, av in annual_values.items():
        dv = dec_values.get(k)
        if av is not None and dv is not None and abs(av - dv) > 0.5:
            mism += 1
            if mism <= 3:
                res.warnings.append(f"annual row {k} = {av} differs from December value {dv}")
    res.meta.update({"sheet": sheet_name, "year_rows": n_year_rows, "annual_vs_december_mismatches": mism})
    if not res.observations:
        res.errors.append("no month rows recognised")
    return res
