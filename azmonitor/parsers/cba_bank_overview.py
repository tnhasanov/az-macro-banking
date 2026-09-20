"""Parser for CBA bank-overview tables (5.1–5.8): labelled rows x month-end date columns.

`cols_per_date: 2` (Table 5.2) means each date has a total column and an FX column.
Header dates may be datetimes or strings such as "30.04.2020 **" (marker retained as a flag).
"""
from __future__ import annotations

import datetime as dt
import re
from pathlib import Path
from typing import Any

from ..util.numbers import parse_number
from ..util.periods import parse_ddmmyyyy, parse_iso_date
from .base import Observation, ParseResult, ParserError
from .xl import cell_ref, find_sheet, load_sheets


def _hdr_date(v: Any) -> tuple[dt.date | None, list[str]]:
    flags: list[str] = []
    if isinstance(v, dt.datetime):
        return v.date(), flags
    if isinstance(v, dt.date):
        return v, flags
    if isinstance(v, str):
        d = parse_ddmmyyyy(v) or parse_iso_date(v)
        if d and "*" in v:
            flags.append("header_marker:" + v.strip())
        return d, flags
    return None, flags


def parse(path: Path, spec: dict[str, Any], *, source_id: str, dataset_id: str, context: dict[str, Any] | None = None) -> ParseResult:
    res = ParseResult()
    sheets = load_sheets(path)
    sheet_name, rows = find_sheet(sheets, spec["sheet"])
    hdr_idx = int(spec.get("header_row", 6)) - 1
    label_col = int(spec.get("label_col", 0))
    per = int(spec.get("cols_per_date", 1))
    if hdr_idx >= len(rows):
        raise ParserError(f"{sheet_name}: header row {hdr_idx+1} missing")
    header = rows[hdr_idx]
    dates: list[tuple[int, dt.date, list[str]]] = []
    for j, v in enumerate(header):
        if j == label_col:
            continue
        d, flags = _hdr_date(v)
        if d:
            dates.append((j, d, flags))
    if not dates:
        raise ParserError(f"{sheet_name}: no date columns found in header row {hdr_idx+1}")
    ptype = spec.get("period_type", "month_end_stock")
    unit = spec.get("unit")
    compiled = [(r, re.compile(r["match"])) for r in spec["rows"]]
    matched: set[str] = set()
    for i in range(hdr_idx + 1, len(rows)):
        row = rows[i]
        if label_col >= len(row):
            continue
        label = str(row[label_col] or "").strip()
        if not label:
            continue
        for rspec, rx in compiled:
            if rspec["id"] in matched or not rx.search(label):
                continue
            matched.add(rspec["id"])
            scale = float(rspec.get("scale", 1.0))
            for j, d, hflags in dates:
                for k in range(per):
                    col = j + k
                    if col >= len(row):
                        continue
                    p = parse_number(row[col])
                    dims = {}
                    if per == 2:
                        dims["currency"] = "all" if k == 0 else "fx"
                    val = p.value * scale if p.value is not None else None
                    pstart = dt.date(d.year, 1, 1) if ptype == "ytd_flow" else None
                    res.observations.append(Observation(
                        series_id=rspec["id"], period_end=d, period_start=pstart, value=val, value_raw=p.raw, freq="M", period_type=ptype,
                        dims=dims, missing_reason=p.missing_reason, unit=rspec.get("unit", unit), scale_note=(f"x{scale}" if scale != 1 else None),
                        population=spec.get("population"), source_id=source_id, sheet=sheet_name, cell_ref=cell_ref(sheet_name, i, col),
                        label_original=label, extraction_method="xlsx_cell", flags=p.flags + hflags,
                        preliminary=any("header_marker" in f for f in hflags),
                    ))
            break
    missing = [r["id"] for r in spec["rows"] if r["id"] not in matched]
    if missing:
        res.warnings.append(f"rows not found: {missing}")
    res.meta.update({"sheet": sheet_name, "n_dates": len(dates), "first_date": dates[0][1].isoformat(), "last_date": dates[-1][1].isoformat()})
    if not res.observations:
        res.errors.append("no configured rows matched")
    return res
