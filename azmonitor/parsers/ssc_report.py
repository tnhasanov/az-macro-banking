"""Parser for the SSC monthly report PDF ("Sosial, iqtisadi inkişaf").

Extracts, from the text layer (pdftotext -layout when available, else pdfplumber):
  Table 1  headline indicators (same rows as the HTML table; used for reconciliation)
  Table 2  GDP by activity, YTD nominal and real index vs previous year
  Table 3  oil-gas / non-oil-gas GDP, YTD nominal and real index
  CPI row of the price-index table (m/m, y/y, YTD)
  Household deposits table (total, AZN, FX)
"""
from __future__ import annotations

import datetime as dt
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

from ..util.numbers import parse_number
from ..util.periods import az_month_number, month_end
from .base import Observation, ParseResult, ParserError
from .ssc_headline import HEADLINE_ROWS, SECTION_PATTERNS, normalise_label, period_from_label

NUM = r"[+-]?\d[\d\s]*(?:,\d+)?(?:\s?d\.)?%?|x|…"
NUM_RE = re.compile(r"(?<![\w,])([+-]?(?:\d{1,3}(?:[ \u00a0]\d{3})+|\d+)(?:,\d+)?(?:\s?d\.)?%?|(?<!\w)x(?!\w)|…)(?![\w,])")

GDP_T2_ROWS = [
    ("total", r"^Ümumi Daxili Məhsul$"),
    ("agriculture", r"^Kənd təsərrüfatı, meşə"),
    ("industry", r"^Sənaye$"),
    ("mining", r"^mədənçıxarma sənayesi"),
    ("manufacturing", r"^emal sənayesi"),
    ("electricity", r"^elektrik enerjisi"),
    ("water", r"^su təchizatı"),
    ("construction", r"^Tikinti$"),
    ("trade", r"^Ticarət: nəqliyyat"),
    ("transport", r"^Nəqliyyat və anbar"),
    ("accommodation", r"^Turistlərin yerləşdirilməsi"),
    ("ict", r"^İnformasiya və rabitə"),
    ("other", r"^Digər sahələr"),
    ("net_taxes", r"^Məhsula və idxala xalis vergilər"),
]
GDP_T3_ROWS = [("total", r"^Ümumi [Dd]axili [Mm]əhsul"), ("oil", r"^[Nn]eft-qaz sektoru"), ("non_oil", r"^[Qq]eyri[- ]neft-qaz sektoru")]


def pdf_text(path: Path) -> str:
    if shutil.which("pdftotext"):
        out = subprocess.run(["pdftotext", "-layout", str(path), "-"], capture_output=True, text=True, timeout=180)
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout
    import pdfplumber

    parts = []
    with pdfplumber.open(str(path)) as pdf:
        for page in pdf.pages:
            parts.append(page.extract_text(layout=True) or "")
    return "\n".join(parts)


def _numbers_after(line: str, label_end: int) -> list[str]:
    tail = line[label_end:]
    return [m.group(1).strip() for m in NUM_RE.finditer(tail)]


def _period_from_title(text: str) -> dt.date | None:
    m = re.search(r"(\d{4})-c[iıuü] İLİN YANVAR-([A-ZƏIİÖÜŞÇĞ]+) AYLARINDA", text)
    if m:
        mon = az_month_number(m.group(2).lower().replace("İ", "i"))
        if mon:
            return month_end(int(m.group(1)), mon)
    m = re.search(r"(\d{4})-c[iıuü] ilin yanvar-([a-zəığöşüçİ]+) aylarında", text, re.IGNORECASE)
    if m:
        mon = az_month_number(m.group(2))
        if mon:
            return month_end(int(m.group(1)), mon)
    return None


def _join_wrapped(lines: list[str]) -> list[str]:
    """Join label continuation lines (a line without numbers followed by a line with numbers and an indented label)."""
    out: list[str] = []
    for ln in lines:
        s = ln.rstrip()
        if out and s and not NUM_RE.search(out[-1]) and out[-1].strip() and not out[-1].strip().endswith((":", "manat", "dolları")) \
                and s.strip() and s.strip()[0].islower() and NUM_RE.search(s):
            out[-1] = out[-1].rstrip() + " " + s.strip()
        else:
            out.append(s)
    return out


def parse_text(text: str, *, source_id: str, edition_period: dt.date | None = None) -> ParseResult:
    res = ParseResult()
    detected = _period_from_title(text)
    head_period = edition_period or detected
    if not head_period:
        raise ParserError("report period not found in title")
    if edition_period and detected and edition_period != detected:
        res.warnings.append(f"period from page title {edition_period} used; text detection gave {detected}")
    pstart = dt.date(head_period.year, 1, 1)
    lines = text.splitlines()

    # ---- Table 1 --------------------------------------------------------------------
    t1_start = next((i for i, l in enumerate(lines) if "Əsas Makroiqtisadi Göstəricilərin İcmalı" in l), None)
    t1_end = next((i for i, l in enumerate(lines) if i > (t1_start or 0) and re.search(r"REAL SEKTOR|Ümumi Daxili Məhsul istehsalı", l)), None)
    section_override: dt.date | None = None
    matched = 0
    if t1_start is not None:
        block = _join_wrapped(lines[t1_start:(t1_end or t1_start + 120)])
        pending_row = None
        for i, raw in enumerate(block):
            s = raw.strip()
            if not s:
                continue
            label_part = re.split(r"\s{2,}(?=[+-]?\d|x\b|…)", s, maxsplit=1)[0]
            if re.match(r"^[+-]?\d|^x\b|^…", s):   # numbers-only continuation line of a wrapped label
                label_part = ""
            label = normalise_label(label_part)
            for sec, pat in SECTION_PATTERNS.items():
                if re.search(pat, label):
                    ov = period_from_label(label)
                    if not ov and i + 1 < len(block):
                        ov = period_from_label(block[i + 1])
                    section_override = ov[1] if ov else None
            nums = _numbers_after(s, len(label_part))
            for row in HEADLINE_ROWS:
                if not re.search(row.pattern, label):
                    continue
                if len(nums) < 2:
                    pending_row = (row, label, i)
                    break
                ov = period_from_label(label)
                if not ov and i + 1 < len(block):
                    ov = period_from_label(block[i + 1])
                if ov:
                    pend = ov[1]
                    if row.section:
                        section_override = ov[1]
                elif row.section and section_override:
                    pend = section_override
                else:
                    pend = head_period
                level, growth = parse_number(nums[0]), parse_number(nums[1])
                _emit_headline(res, row, level, growth, pend, source_id, f"pdf:table1:l{t1_start + i + 1}", label)
                matched += 1
                break
            else:
                if pending_row and nums and len(nums) >= 2:
                    row, label, _ = pending_row
                    pend = section_override if (row.section and section_override) else head_period
                    ov = period_from_label(s)
                    if not ov and i + 1 < len(block):
                        ov = period_from_label(block[i + 1])   # note printed under the numbers line
                    if ov:
                        pend = ov[1]
                        if row.section:
                            section_override = ov[1]
                    _emit_headline(res, row, parse_number(nums[0]), parse_number(nums[1]), pend, source_id, f"pdf:table1:l{t1_start + i + 1}", label)
                    matched += 1
                    pending_row = None
    if matched == 0 and t1_start is not None:
        matched = _parse_legacy_table1(lines[t1_start:(t1_end or t1_start + 140)], head_period, res, source_id, t1_start)
    res.meta["table1_rows"] = matched

    # ---- Table 2 / 3 GDP by activity ----------------------------------------------------
    def parse_gdp_table(anchor: str, rows_spec, prefix: str, tag: str):
        start = next((i for i, l in enumerate(lines) if anchor in l), None)
        if start is None:
            res.warnings.append(f"{tag}: anchor not found")
            return
        block = _join_wrapped(lines[start:start + 70])
        done = set()
        for i, raw in enumerate(block):
            s = raw.strip()
            if not s:
                continue
            label_part = re.split(r"\s{2,}(?=[+-]?\d|x\b|…)", s, maxsplit=1)[0]
            label = " ".join(label_part.split())
            for key, pat in rows_spec:
                if key in done or not re.search(pat, label):
                    continue
                nums = _numbers_after(s, len(label_part))
                if len(nums) < 3:
                    continue
                cur, prev, idx = parse_number(nums[0]), parse_number(nums[1]), parse_number(nums[2])
                ref = f"pdf:{tag}:l{start + i + 1}"
                res.observations.append(Observation(series_id=f"{prefix}.{key}.nominal_ytd", period_end=head_period, period_start=pstart, value=cur.value,
                                                    value_raw=cur.raw, freq="M", period_type="ytd_flow", missing_reason=cur.missing_reason, unit="AZN mln",
                                                    basis="current prices", source_id=source_id, cell_ref=ref + ":cur", label_original=label,
                                                    extraction_method="pdf_text", flags=cur.flags))
                res.observations.append(Observation(series_id=f"{prefix}.{key}.nominal_ytd_prev_year", period_end=head_period, period_start=pstart, value=prev.value,
                                                    value_raw=prev.raw, freq="M", period_type="ytd_flow_prev_year_comparable", missing_reason=prev.missing_reason,
                                                    unit="AZN mln", basis="previous-year comparable, as published in this edition (may be revised)",
                                                    source_id=source_id, cell_ref=ref + ":prev", label_original=label, extraction_method="pdf_text", flags=prev.flags))
                res.observations.append(Observation(series_id=f"{prefix}.{key}.real_index_ytd", period_end=head_period, period_start=pstart, value=idx.value,
                                                    value_raw=idx.raw, freq="M", period_type="ytd_index_vs_prev_year", missing_reason=idx.missing_reason,
                                                    unit="index, % of previous year", basis="comparable prices", source_id=source_id, cell_ref=ref + ":idx",
                                                    label_original=label, extraction_method="pdf_text", flags=idx.flags))
                done.add(key)
                break

    parse_gdp_table("Ümumi daxili məhsul istehsalı", GDP_T2_ROWS, "ssc.gdp.sector", "table2")
    parse_gdp_table("Neft-qaz və qeyri-neft-qaz sektorlarında ümumi", GDP_T3_ROWS, "ssc.gdp", "table3")

    # ---- CPI row in the price-index table ------------------------------------------------
    for i, l in enumerate(lines):
        if re.match(r"^\s*İstehlak qiymətləri indeksi\s{2,}\d", l):
            nums = NUM_RE.findall(l)
            if len(nums) >= 3:
                mom, yoy, ytd = (parse_number(n) for n in nums[:3])
                for sid, p, pt, ps in [("ssc.cpi.all.mom_index", mom, "monthly_index_vs_prev_month", head_period.replace(day=1)),
                                       ("ssc.cpi.all.yoy_index", yoy, "monthly_index_vs_same_month_prev_year", head_period.replace(day=1)),
                                       ("ssc.cpi.all.ytd_index", ytd, "ytd_average_index_vs_prev_year", pstart)]:
                    res.observations.append(Observation(series_id=sid, period_end=head_period, period_start=ps, value=p.value, value_raw=p.raw, freq="M",
                                                        period_type=pt, missing_reason=p.missing_reason, unit="index, % of base", source_id=source_id,
                                                        cell_ref=f"pdf:prices:l{i + 1}", label_original="İstehlak qiymətləri indeksi", extraction_method="pdf_text"))
            break

    # ---- household deposits table (AZN / FX) ---------------------------------------------
    start = None
    for i, l in enumerate(lines):
        if "Fiziki şəxslərin banklardakı əmanətləri" in l and "Cədvəl" not in l:
            window = "\n".join(lines[i:i + 30])
            if "Əmanətlər" in window and "cəmi" in window and "vəziyyətinə" in window:
                start = i
                break
    if start is not None:
        blk = lines[start:start + 25]
        stock_date = None
        window = "\n".join(blk)
        m = re.search(r"(\d{4})-c[iıuü] il ([a-zəığöşüçİ]+)", window)
        if m and "1-i" in window and "vəziyyətinə" in window:
            mon = az_month_number(m.group(2))
            if mon:
                d = dt.date(int(m.group(1)), mon, 1)
                stock_date = d - dt.timedelta(days=1)
        if stock_date:
            for i, l in enumerate(blk):
                for key, pat in [("total", r"^\s*Əmanətlər\s*[–-]\s*cəmi"), ("azn", r"^\s*milli valyutada"), ("fx", r"^\s*xarici valyutada")]:
                    if re.search(pat, l):
                        nums = NUM_RE.findall(l)
                        if len(nums) >= 2:
                            lvl, idx = parse_number(nums[0]), parse_number(nums[1])
                            res.observations.append(Observation(series_id=f"ssc.hl.hh_deposits.{key}.level", period_end=stock_date, value=lvl.value, value_raw=lvl.raw,
                                                                freq="M", period_type="month_end_stock", missing_reason=lvl.missing_reason, unit="AZN mln",
                                                                source_id=source_id, cell_ref=f"pdf:deposits:l{start + i + 1}", label_original=l.strip()[:60],
                                                                extraction_method="pdf_text", basis="SSC republication of CBA data"))
                            res.observations.append(Observation(series_id=f"ssc.hl.hh_deposits.{key}.index_yoy", period_end=stock_date, value=idx.value, value_raw=idx.raw,
                                                                freq="M", period_type="stock_index_vs_prev_year", missing_reason=idx.missing_reason,
                                                                unit="index, % of previous year", source_id=source_id, cell_ref=f"pdf:deposits:l{start + i + 1}",
                                                                label_original=l.strip()[:60], extraction_method="pdf_text"))
    res.meta["headline_period_end"] = head_period.isoformat()
    n_gdp = sum(1 for o in res.observations if o.series_id.startswith("ssc.gdp."))
    if matched < 10 and n_gdp < 10:
        res.errors.append(f"table 1: only {matched} rows matched and GDP tables not found")
    elif matched < 10:
        res.warnings.append(f"table 1: only {matched} rows matched (headline table covered by the HTML edition page)")
    return res


def _parse_legacy_table1(block_lines: list[str], head_period: dt.date, res: ParseResult, source_id: str, offset: int) -> int:
    """Legacy PDF layout (editions before November 2025): labels wrap over two lines, growth is an index (101,4)."""
    from .ssc_html import LEGACY_ROWS, _growth_from_index, _level_with_stars, _mk
    from ..util.periods import shift_months
    text = "\n".join(block_lines)
    star_period = shift_months(head_period, -1)
    m = re.search(r"\*\s*(\d{4})-c[iıuü] ilin yanvar-([a-zəığöşüçİ]+) ayları", text)
    if m:
        mon = az_month_number(m.group(2))
        if mon:
            star_period = month_end(int(m.group(1)), mon)
    # merge wrapped labels: a line without numbers followed by a line with numbers
    merged: list[tuple[str, list[str], int]] = []
    carry = ""
    for i, raw in enumerate(block_lines):
        s = raw.strip()
        if not s:
            carry = ""
            continue
        label_part = re.split(r"\s{2,}(?=[+-]?\d|x\b|…)", s, maxsplit=1)[0]
        nums = [mm.group(1).strip() for mm in NUM_RE.finditer(s[len(label_part):])]
        if nums:
            merged.append(((carry + " " + label_part).strip(), nums, i))
            carry = ""
        else:
            carry = (carry + " " + label_part).strip() if len(label_part) < 60 else ""
    n = 0
    trade_key = None
    for label_raw, nums, i in merged:
        label = normalise_label(label_raw)
        for key, pat in [("trade_turnover", r"^Xarici ticarət dövriyyəsi"), ("exports", r"^o cümlədən: ixrac"), ("exports_nonoil", r"^ondan qeyri[- ]neft-qaz ixracı"), ("imports", r"^idxal")]:
            if re.search(pat, label):
                trade_key = key
        if trade_key and label.startswith("faktiki qiymətlərlə") and len(nums) >= 2:
            level, s1 = _level_with_stars(nums[0])
            growth, s2 = _growth_from_index(nums[1])
            _mk(res, trade_key, "ytd_flow", "USD mln", level, growth, star_period if (s1 or s2) else head_period, source_id, f"pdf:table1:l{offset + i + 1}", label,
                "legacy layout (PDF)", method="pdf_text")
            n += 1
            trade_key = None if trade_key == "imports" else trade_key
            continue
        for key, pat, kind, unit in LEGACY_ROWS:
            if not re.search(pat, label) or len(nums) < 2:
                continue
            level, s1 = _level_with_stars(nums[0])
            growth, s2 = _growth_from_index(nums[1])
            basis = "legacy layout (PDF)"
            if kind == "stock":
                sm = re.search(r"(\d{4})-c[iıuü] il 0?(\d{1,2}) ([a-zəığöşüçİ]+) vəziyyətinə", label)
                if sm:
                    mon = az_month_number(sm.group(3))
                    d = dt.date(int(sm.group(1)), mon, int(sm.group(2))) if mon else None
                    pend = (d - dt.timedelta(days=1)) if d and d.day == 1 else (d or head_period)
                else:
                    pend = head_period
            elif key == "cpi_yoy_month":
                pend = head_period
            elif s1 or s2:
                pend = star_period
            else:
                pend = head_period
            _mk(res, key, kind, unit, level if kind != "growth_only" else None, growth, pend, source_id, f"pdf:table1:l{offset + i + 1}", label, basis, method="pdf_text")
            n += 1
            break
    return n


def _emit_headline(res: ParseResult, row, level, growth, pend: dt.date, source_id: str, ref: str, label: str) -> None:
    pstart = dt.date(pend.year, 1, 1)
    ptype_level = {"ytd_flow": "ytd_flow", "ytd_average": "ytd_average", "stock": "month_end_stock", "growth_only": "ytd_growth_yoy"}[row.kind]
    if row.kind != "growth_only":
        res.observations.append(Observation(series_id=f"ssc.hl.{row.key}.level", period_end=pend, period_start=None if row.kind == "stock" else pstart,
                                            value=level.value, value_raw=level.raw, freq="M", period_type=ptype_level, missing_reason=level.missing_reason,
                                            unit=row.unit, source_id=source_id, cell_ref=ref + ":B", label_original=label, extraction_method="pdf_text", flags=level.flags))
    gtype = "stock_growth_yoy" if row.kind == "stock" else "ytd_growth_yoy"
    res.observations.append(Observation(series_id=f"ssc.hl.{row.key}.growth", period_end=pend, period_start=None if row.kind == "stock" else pstart,
                                        value=growth.value, value_raw=growth.raw, freq="M", period_type=gtype, missing_reason=growth.missing_reason, unit="%",
                                        source_id=source_id, cell_ref=ref + ":C", label_original=label, extraction_method="pdf_text", flags=growth.flags))


def parse(path: Path, spec: dict[str, Any], *, source_id: str, dataset_id: str, context: dict[str, Any] | None = None) -> ParseResult:
    text = pdf_text(Path(path))
    if len(text) < 2000:
        raise ParserError("PDF text layer too short; scanned document? OCR is not enabled")
    ep = (context or {}).get("edition_period")
    if isinstance(ep, str):
        ep = dt.date.fromisoformat(ep)
    return parse_text(text, source_id=source_id, edition_period=ep)
