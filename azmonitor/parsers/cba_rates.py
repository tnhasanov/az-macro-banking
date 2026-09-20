"""Parser for CBA interest-rate tables (3.2, 3.2.1): a date row followed by currency rows.

    r281  | 2026-08-01 |      |       |
    r282  | Manatla    | 7.46 | 18.96 |
    r283  | Xarici valyuta ilə | 2.98 | 7.07 |

Date convention (configured `date_convention: first_of_following_month`): the CBA labels
the block "as at the 1st of the following month", so 2026-08-01 refers to July 2026.
"""
from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Any

from ..util.numbers import parse_number
from ..util.periods import parse_ddmmyyyy, parse_iso_date, prev_month_end
from .base import Observation, ParseResult
from .cba_matrix import check_header
from .xl import cell_ref, find_sheet, load_sheets


def _as_date(v: Any) -> dt.date | None:
    if isinstance(v, dt.datetime):
        return v.date()
    if isinstance(v, dt.date):
        return v
    if isinstance(v, str):
        return parse_ddmmyyyy(v) or parse_iso_date(v)
    return None


def parse(path: Path, spec: dict[str, Any], *, source_id: str, dataset_id: str, context: dict[str, Any] | None = None) -> ParseResult:
    res = ParseResult()
    sheets = load_sheets(path)
    sheet_name, rows = find_sheet(sheets, spec["sheet"])
    check_header(rows, spec, sheet_name)
    date_col = int(spec.get("date_col", 1))
    label_col = int(spec.get("label_col", 1))
    cur_rows: dict[str, str] = spec.get("currency_rows", {"Manatla": "AZN", "Xarici valyuta ilə": "FX"})
    ptype = spec.get("period_type", "monthly_average_rate")
    conv = spec.get("date_convention", "first_of_following_month")
    current: dt.date | None = None
    hdr_row = int(spec.get("header_check", {}).get("row", 6))
    for i in range(hdr_row, len(rows)):
        row = rows[i]
        if max(date_col, label_col) >= len(row):
            continue
        d = _as_date(row[date_col])
        if d is not None:
            current = d
            continue
        label = str(row[label_col] or "").strip()
        if not label or current is None:
            continue
        cur = None
        for k, v in cur_rows.items():
            if label.lower().startswith(k.lower()):
                cur = v
                break
        if cur is None:
            continue
        if conv == "first_of_following_month" and current.day == 1:
            pend = prev_month_end(current)
        else:
            pend = current
        pstart = pend.replace(day=1)
        for s in spec["series"]:
            col = int(s["col"])
            if col >= len(row):
                continue
            p = parse_number(row[col])
            res.observations.append(Observation(
                series_id=s["id"], period_end=pend, period_start=pstart, value=p.value, value_raw=p.raw, freq="M", period_type=ptype,
                dims={"currency": cur}, missing_reason=p.missing_reason, unit=s.get("unit", spec.get("unit")), currency=cur,
                population=s.get("population", spec.get("population", "all_credit_institutions")), source_id=source_id, sheet=sheet_name,
                cell_ref=cell_ref(sheet_name, i, col), label_original=label, extraction_method="xlsx_cell", flags=p.flags,
                basis=f"date label {current.isoformat()} interpreted as reference month {pend:%Y-%m}",
            ))
    if not res.observations:
        res.errors.append("no rate blocks recognised")
    res.meta["sheet"] = sheet_name
    return res
