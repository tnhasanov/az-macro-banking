"""Parser for CBA regional snapshot tables (2.10 loans by region, 2.14 savings by region).

The sheets carry no reference date. The pipeline supplies `context["period_end"]` from the
companion monthly table published in the same batch (configured `period_from_dataset`) and
validates the national total against that table. Without a resolvable period the parser
refuses to guess.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from ..util.numbers import parse_number
from .base import Observation, ParseResult, ParserError
from .cba_matrix import check_header
from .xl import cell_ref, find_sheet, load_sheets


def parse(path: Path, spec: dict[str, Any], *, source_id: str, dataset_id: str, context: dict[str, Any] | None = None) -> ParseResult:
    res = ParseResult()
    context = context or {}
    pend = context.get("period_end")
    if pend is None:
        raise ParserError("regional snapshot: reference period not supplied (period_from_dataset unresolved)")
    sheets = load_sheets(path)
    sheet_name, rows = find_sheet(sheets, spec["sheet"])
    check_header(rows, spec, sheet_name)
    label_col = int(spec.get("label_col", 0))
    scale_default = float(spec.get("scale", 1.0))
    national = spec.get("national_label", "Azərbaycan Respublikası")
    hdr_row = int(spec.get("header_check", {}).get("row", 6))
    for i in range(hdr_row, len(rows)):
        row = rows[i]
        if label_col >= len(row):
            continue
        label = str(row[label_col] or "").strip()
        if not label or label.lower().startswith(("o cümlədən", "mənbə", "qeyd")):
            continue
        region = "national" if label == national else label
        for s in spec["series"]:
            col = int(s["col"])
            if col >= len(row):
                continue
            p = parse_number(row[col])
            scale = float(s.get("scale", scale_default))
            val = p.value * scale if p.value is not None else None
            res.observations.append(Observation(
                series_id=s["id"], period_end=pend, value=val, value_raw=p.raw, freq="M", period_type=spec.get("period_type", "month_end_stock"),
                dims={"region": region}, missing_reason=p.missing_reason, unit=s.get("unit", spec.get("unit")), scale_note=f"x{scale}",
                population=spec.get("population"), source_id=source_id, sheet=sheet_name, cell_ref=cell_ref(sheet_name, i, col),
                label_original=label, extraction_method="xlsx_cell", flags=p.flags,
                basis=f"period inferred from companion table {spec.get('period_from_dataset')} in the same publication batch",
            ))
    if not any(o.dims.get("region") == "national" for o in res.observations):
        res.errors.append("national total row not found")
    res.meta.update({"sheet": sheet_name, "period_end": pend.isoformat()})
    return res
