"""Parsers for SSC open-data workbooks (CPI bulletin, CPI index, quarterly GDP, annual tables)."""
from __future__ import annotations

import datetime as dt
import re
from pathlib import Path
from typing import Any

from ..util.numbers import parse_number
from ..util.periods import az_lower, az_month_number, month_end, quarter_end
from .base import Observation, ParseResult, ParserError
from .xl import cell_ref, find_sheet, load_sheets

ROMAN = {"I": 1, "II": 2, "III": 3, "IV": 4, "V": 5, "VI": 6, "VII": 7, "VIII": 8, "IX": 9, "X": 10, "XI": 11, "XII": 12}

# Row labels in the price bulletin (first column) -> series suffix; matched on normalised prefixes
CPI_GROUPS = [
    ("cəmi mallar və xidmətlər", "all"),
    ("qida malları, içkilər", "food"),
    ("qeyri-qida malları, xidmətlər", None),        # goods+services aggregate: skipped
    ("qeyri-qida malları", "nonfood"),
    ("xidmətlər", "services"),
]


def cpi_group(label: str) -> str | None:
    key = " ".join(az_lower(label).split())
    for prefix, g in CPI_GROUPS:
        if key.startswith(prefix):
            return g
    return None


def _year_from_filename(path: Path) -> int | None:
    m = re.search(r"(20\d{2})", path.name)
    return int(m.group(1)) if m else None


def _year_from_text(text: str) -> int | None:
    m = re.search(r"(20\d{2})", text)
    return int(m.group(1)) if m else None


def parse_price_bulletin(path: Path, spec: dict[str, Any], *, source_id: str, dataset_id: str, context: dict[str, Any] | None = None) -> ParseResult:
    """SSC 'Qiymətlər və qiymət indeksləri' bulletin: sheets for m/m, y/y and YTD-average CPI by group.
    Values are indices in percent of the comparison base (100,2 = +0.2%)."""
    res = ParseResult()
    context = context or {}
    sheets = load_sheets(path)
    year = context.get("year") or _year_from_text(context.get("title", "")) or _year_from_filename(path)
    if not year:
        raise ParserError("price bulletin: reference year not determinable from title/filename")
    plans = [
        ("əvvəlki aya", "ssc.cpi.{g}.mom_index", "monthly_index_vs_prev_month"),
        ("əvvəlki ilin müvafiq ayına", "ssc.cpi.{g}.yoy_index", "monthly_index_vs_same_month_prev_year"),
        ("əvvəlki ilin müvafiq dövrünə", "ssc.cpi.{g}.ytd_index", "ytd_average_index_vs_prev_year"),
    ]
    for key, sid_tpl, ptype in plans:
        sname = None
        for k in sheets:
            if k.strip().lower() == key:
                sname = k
                break
        if sname is None:
            res.warnings.append(f"sheet {key!r} not found")
            continue
        rows = sheets[sname]
        # header row with month names
        hdr_i = None
        for i, r in enumerate(rows[:10]):
            txt = [str(v or "").strip() for v in r]
            if any(t.lower().startswith("yanvar") for t in txt):
                hdr_i = i
                break
        if hdr_i is None:
            res.warnings.append(f"{sname}: month header not found")
            continue
        month_cols: list[tuple[int, int]] = []
        for j, v in enumerate(rows[hdr_i]):
            t = str(v or "").strip()
            if not t:
                continue
            # "Yanvar-avqust" -> take the last month for YTD sheets
            last = t.split("-")[-1].strip()
            m = az_month_number(last)
            if m:
                month_cols.append((j, m))
        for i in range(hdr_i + 1, len(rows)):
            r = rows[i]
            label = str(r[1] if len(r) > 1 else "").strip()
            g = cpi_group(label)
            if not g:
                continue
            for j, m in month_cols:
                if j >= len(r):
                    continue
                p = parse_number(r[j])
                pend = month_end(year, m)
                pstart = dt.date(year, 1, 1) if ptype.startswith("ytd") else pend.replace(day=1)
                res.observations.append(Observation(
                    series_id=sid_tpl.format(g=g), period_end=pend, period_start=pstart, value=p.value, value_raw=p.raw, freq="M",
                    period_type=ptype, missing_reason=p.missing_reason, unit="index, % of base", source_id=source_id, sheet=sname,
                    cell_ref=cell_ref(sname, i, j), label_original=label, extraction_method="xls_cell", flags=p.flags,
                ))
    if not res.observations:
        res.errors.append("no CPI rows recognised")
    res.meta["year"] = year
    return res


def parse_cpi_index(path: Path, spec: dict[str, Any], *, source_id: str, dataset_id: str, context: dict[str, Any] | None = None) -> ParseResult:
    """Table 1.2: CPI (2010=100) by year block and Roman-numeral month rows."""
    res = ParseResult()
    sheets = load_sheets(path)
    sname, rows = next(iter(sheets.items()))
    cols = {2: "all", 3: "food", 4: "nonfood", 5: "services"}
    year = None
    for i, r in enumerate(rows):
        key = str(r[1] if len(r) > 1 else "").strip()
        if re.fullmatch(r"(19|20)\d{2}", key):
            year = int(key)
            continue
        if key in ROMAN and year:
            m = ROMAN[key]
            for j, g in cols.items():
                if j >= len(r):
                    continue
                p = parse_number(r[j])
                res.observations.append(Observation(
                    series_id=f"ssc.cpi.{g}.index_2010", period_end=month_end(year, m), value=p.value, value_raw=p.raw, freq="M",
                    period_type="monthly_index", missing_reason=p.missing_reason, unit="index 2010=100", source_id=source_id, sheet=sname,
                    cell_ref=cell_ref(sname, i, j), label_original=key, extraction_method="xlsx_cell", flags=p.flags,
                ))
    if not res.observations:
        res.errors.append("no CPI index rows recognised")
    return res


GDP_SECTOR_ROWS = {
    "Kənd təsərrüfatı": "agriculture",
    "Mədənçıxarma": "mining",
    "Emal sənayesi": "manufacturing",
    "Elektrik enerjisi": "electricity",
    "Su təchizatı": "water",
    "Tikinti": "construction",
    "Ticarət": "trade",
    "Nəqliyyat": "transport",
    "Turistlərin yerləşdirilməsi": "accommodation",
    "İnformasiya və rabitə": "ict",
    "Xalis vergilər": "net_taxes",
    "Ümumi daxili məhsul": "total",
}


def parse_gdp_quarterly(path: Path, spec: dict[str, Any], *, source_id: str, dataset_id: str, context: dict[str, Any] | None = None) -> ParseResult:
    """Table 03r: GDP by quarter and activity; sheet 'cari qiy.' current prices, '2005 qiy.' constant 2005 prices."""
    res = ParseResult()
    sheets = load_sheets(path)
    for sname, basis, suffix in [("cari qiy.", "current prices", "nominal_q"), ("2005 qiy.", "constant 2005 prices", "real2005_q")]:
        try:
            sname, rows = find_sheet(sheets, sname)
        except KeyError:
            res.warnings.append(f"sheet {sname!r} missing")
            continue
        year_row = q_row = None
        for i, r in enumerate(rows[:12]):
            vals = [v for v in r if v not in (None, "")]
            if any(isinstance(v, (int, float)) and 1990 < float(v) < 2100 for v in vals) and year_row is None:
                year_row = i
            if any(isinstance(v, str) and "rüb" in v.lower() for v in vals):
                q_row = i
        if year_row is None or q_row is None:
            res.warnings.append(f"{sname}: year/quarter header not found")
            continue
        colmap: dict[int, dt.date] = {}
        cur_year = None
        for j in range(len(rows[q_row])):
            yv = rows[year_row][j] if j < len(rows[year_row]) else None
            if isinstance(yv, (int, float)) and 1990 < float(yv) < 2100:
                cur_year = int(yv)
            qv = str(rows[q_row][j] or "").strip()
            m = re.match(r"^(I|II|III|IV)\s*rüb", qv)
            if m and cur_year:
                colmap[j] = quarter_end(cur_year, ROMAN[m.group(1)])
        for i in range(q_row + 1, len(rows)):
            r = rows[i]
            label = str(r[1] if len(r) > 1 else "").strip()
            key = next((v for k, v in GDP_SECTOR_ROWS.items() if label.startswith(k)), None)
            if not key:
                continue
            for j, pend in colmap.items():
                if j >= len(r):
                    continue
                p = parse_number(r[j])
                res.observations.append(Observation(
                    series_id=f"ssc.gdp.sector.{key}.{suffix}", period_end=pend, period_start=dt.date(pend.year, pend.month - 2, 1), value=p.value,
                    value_raw=p.raw, freq="Q", period_type="quarterly_flow", missing_reason=p.missing_reason, unit="AZN mln", basis=basis,
                    source_id=source_id, sheet=sname, cell_ref=cell_ref(sname, i, j), label_original=label, extraction_method="xls_cell", flags=p.flags,
                ))
    if not res.observations:
        res.errors.append("no quarterly GDP rows recognised")
    return res


def parse_gdp_oil_nonoil_annual(path: Path, spec: dict[str, Any], *, source_id: str, dataset_id: str, context: dict[str, Any] | None = None) -> ParseResult:
    """Table 7.1: annual GDP total / oil-gas / non-oil-gas at current prices (block 1) and real index (block 2)."""
    res = ParseResult()
    sheets = load_sheets(path)
    sname, rows = next(iter(sheets.items()))
    label_map = {"Ümumi daxili məhsul": "total", "neft-qaz sektoru": "oil", "qeyri neft-qaz sektoru": "non_oil", "məhsula və idxala": "net_taxes"}
    block = 0
    year_cols: dict[int, int] = {}
    for i, r in enumerate(rows):
        ys = {j: int(v) for j, v in enumerate(r) if isinstance(v, (int, float)) and 1990 < float(v) < 2100 and float(v).is_integer()}
        if len(ys) >= 5:
            year_cols = ys
            block += 1
            continue
        label = str(r[1] if len(r) > 1 else "").strip()
        key = next((v for k, v in label_map.items() if az_lower(label).startswith(az_lower(k))), None)
        if not key or not year_cols:
            continue
        suffix = "nominal_a" if block == 1 else "real_index_a"
        for j, y in year_cols.items():
            if j >= len(r):
                continue
            p = parse_number(r[j])
            res.observations.append(Observation(
                series_id=f"ssc.gdp.{key}.{suffix}", period_end=dt.date(y, 12, 31), period_start=dt.date(y, 1, 1), value=p.value, value_raw=p.raw,
                freq="A", period_type="annual_flow" if block == 1 else "annual_index_vs_prev_year", missing_reason=p.missing_reason,
                unit="AZN mln" if block == 1 else "index, % of previous year", source_id=source_id, sheet=sname, cell_ref=cell_ref(sname, i, j),
                label_original=label, extraction_method="xls_cell", flags=p.flags,
            ))
    if not res.observations:
        res.errors.append("no annual GDP rows recognised")
    return res


def parse_wages_annual(path: Path, spec: dict[str, Any], *, source_id: str, dataset_id: str, context: dict[str, Any] | None = None) -> ParseResult:
    """Table 4.2: average monthly nominal wage by activity, annual (AZN)."""
    res = ParseResult()
    sheets = load_sheets(path)
    sname, rows = find_sheet(sheets, "4.2")
    year_cols: dict[int, int] = {}
    for i, r in enumerate(rows):
        ys = {j: int(v) for j, v in enumerate(r) if isinstance(v, (int, float)) and 1990 < float(v) < 2100 and float(v).is_integer()}
        if len(ys) >= 3 and not year_cols:
            year_cols = ys
            continue
        label = str(r[2] if len(r) > 2 else "").strip()
        if not year_cols or not label:
            continue
        if az_lower(label).startswith("iqtisadiyyat üzrə"):
            key = "total"
        else:
            continue
        for j, y in year_cols.items():
            if j >= len(r):
                continue
            p = parse_number(r[j])
            res.observations.append(Observation(
                series_id=f"ssc.wage.nominal.{key}.annual", period_end=dt.date(y, 12, 31), period_start=dt.date(y, 1, 1), value=p.value, value_raw=p.raw,
                freq="A", period_type="annual_average", missing_reason=p.missing_reason, unit="AZN per month", source_id=source_id, sheet=sname,
                cell_ref=cell_ref(sname, i, j), label_original=label, extraction_method="xls_cell", flags=p.flags,
            ))
    if not res.observations:
        res.errors.append("no wage rows recognised")
    return res
