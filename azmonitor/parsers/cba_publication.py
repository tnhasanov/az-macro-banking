"""Parser for CBA narrative publications (reviews, decisions, policy statements, stability reports).

Returns the usual observations plus the publication record, its passages and, for decisions, the
decision event. The pipeline persists all of them, so a citation in the deck can be followed back to
a page of a specific file.

Reporting period and publication date are treated as different facts throughout. The reporting
period comes from the title when the title states it, and is left unknown otherwise; the publication
date comes from the page, the press release or the file, never from the reporting period.
"""
from __future__ import annotations

import datetime as dt
import re
from pathlib import Path
from typing import Any

from ..publications import policy as pol
from ..publications import stability as stab
from ..publications.editions import EN_MONTHS
from ..publications.extract import extract
from ..util.log import get_logger
from ..util.periods import az_month_number
from .base import ParseResult, ParserError

log = get_logger("pub_parser")

AZ_VINTAGE = re.compile(rf"(\d{{4}})-c[iıuü] ilin\s+({pol.AZ_MONTHS_RE})\s+proqnoz", re.IGNORECASE)
EN_VINTAGE = re.compile(rf"({pol.EN_MONTHS_RE})\s+(\d{{4}})\s+(?:projection|forecast)", re.IGNORECASE)
AZ_VINTAGE_SHORT = re.compile(rf"({pol.AZ_MONTHS_RE})\s+proqnozlar", re.IGNORECASE)


def _forecast_vintage(passages: list[dict[str, Any]], language: str, edition_key: str) -> tuple[str, str]:
    """The forecast round a review publishes, taken from its own wording where it states one."""
    text = " ".join(p["text"] for p in passages[:200])
    m = AZ_VINTAGE.search(text) if language == "az" else EN_VINTAGE.search(text)
    if m:
        if language == "az":
            year, mon = int(m.group(1)), az_month_number(m.group(2))
        else:
            mon, year = EN_MONTHS.get(m.group(1).lower()), int(m.group(2))
        if mon:
            return f"{year:04d}-{mon:02d}", "forecast round named in the publication"
    if language == "az":
        m = AZ_VINTAGE_SHORT.search(text)
        if m and re.match(r"^\d{4}-\d{2}$", edition_key):
            mon = az_month_number(m.group(1))
            if mon:
                year = int(edition_key[:4]) - (1 if mon > int(edition_key[5:7]) else 0)
                return f"{year:04d}-{mon:02d}", "forecast round named in the publication"
    return (edition_key if re.match(r"^\d{4}-\d{2}$", edition_key) else edition_key[:4] + "-12"), \
           "no forecast round stated; the edition month is used and labelled as such"


def parse(path: Path, spec: dict[str, Any], *, source_id: str, dataset_id: str,
          context: dict[str, Any] | None = None) -> ParseResult:
    ctx = context or {}
    pub_type = ctx.get("pub_type") or spec.get("pub_type")
    if not pub_type:
        raise ParserError("publication parser requires a pub_type in the dataset context")
    language = ctx.get("language") or "az"
    doc_id = ctx.get("doc_id") or dataset_id
    publication_id = ctx.get("publication_id") or f"{pub_type}:{ctx.get('edition_key')}"
    extension = ctx.get("extension") or ("html" if str(path).endswith(".html") else "pdf")
    res = ParseResult()
    # Numbers are extracted from one designated language per publication type, so the second
    # language edition cannot restate the same fact with a different rounding or a mis-parse and
    # look like a revision. The other edition is still stored in full and is citable.
    extract_language = spec.get("extract_language", "az")
    numeric = (language == extract_language)

    ex = extract(Path(path), extension, with_tables=bool(spec.get("tables", True)))
    if not ex.passages:
        raise ParserError("no text could be extracted from the publication")
    passages = []
    for p in ex.passages:
        passages.append({"passage_id": p.passage_id(doc_id), "doc_id": doc_id, "publication_id": publication_id,
                         "language": language, "page_index": p.page_index, "printed_page": p.printed_page,
                         "section": p.section, "kind": p.kind, "ord": p.ord, "text": p.text,
                         "extraction_method": p.extraction_method, "validation_status": p.validation_status})
    res.passages = passages
    res.warnings.extend(ex.warnings)

    reporting_start = _as_date(ctx.get("reporting_start"))
    reporting_end = _as_date(ctx.get("reporting_end"))
    edition_key = ctx.get("edition_key") or ""
    edition_label = ctx.get("edition_label") or edition_key
    pub: dict[str, Any] = {
        "publication_id": publication_id, "source_id": source_id, "pub_type": pub_type, "edition_key": edition_key,
        "edition_label": edition_label, "reporting_period_start": ctx.get("reporting_start"),
        "reporting_period_end": ctx.get("reporting_end"), "reporting_frequency": ctx.get("reporting_frequency"),
        "announcement_date": ctx.get("announcement_date"), "status": "extracted",
        "note": ctx.get("period_basis") or ("reporting period not stated in the title" if not reporting_end else None),
    }

    if not numeric:
        res.meta["numeric_extraction"] = f"skipped: numbers are extracted from the {extract_language} edition of this publication type"
    if pub_type == "monetary_policy_review" and numeric:
        vintage, basis = _forecast_vintage(passages, language, edition_key)
        pub["information_cutoff"] = None
        pub["note"] = (pub["note"] or "") + f"; forecast round {vintage} ({basis})"
        rows = pol.parse_decision_table(passages)
        if rows:
            res.observations.extend(pol.decision_observations(rows, publication_id=publication_id, language=language))
            res.decisions = [_decision_record(r, publication_id, doc_id) for r in rows]
        else:
            res.warnings.append("no decision table found in this review")
        vdate = _vintage_date(vintage)
        obs, notes = pol.extract_forecasts(passages, language, publication_id=publication_id, vintage=vintage,
                                           vintage_date=vdate)
        res.observations.extend(obs)
        res.meta["forecast_notes"] = notes
        res.meta["forecast_vintage"] = vintage
        res.meta["forecast_vintage_basis"] = basis
    elif pub_type == "monetary_policy_review":
        vintage, basis = _forecast_vintage(passages, language, edition_key)
        pub["note"] = (pub["note"] or "") + f"; forecast round {vintage} ({basis})"
    elif pub_type == "policy_decision":
        announced = _as_date(ctx.get("announcement_date"))
        rel = pol.parse_press_release(passages, language, announced)
        pub["announcement_date"] = (rel.announcement_date or announced).isoformat() if (rel.announcement_date or announced) else None
        pub["effective_date"] = rel.effective_date.isoformat() if rel.effective_date else None
        pub["next_release_date"] = rel.next_decision_date.isoformat() if rel.next_decision_date else None
        pub["next_release_basis"] = rel.next_decision_basis
        pub["published_at"] = pub["announcement_date"]
        pub["published_at_basis"] = "announcement date of the decision"
        res.decisions = [{
            "decision_id": f"decision:{pub['announcement_date']}", "publication_id": publication_id,
            "announcement_date": pub["announcement_date"], "effective_date": pub["effective_date"],
            "effective_date_basis": rel.effective_basis, "action": rel.action,
            "rationale_text": rel.rationale, "rationale_language": language,
            "next_decision_date": pub["next_release_date"], "next_decision_basis": rel.next_decision_basis,
            "source_doc_id": doc_id, "validation_status": "verified",
        }]
        if announced and numeric:
            vintage = f"{announced.year:04d}-{announced.month:02d}"
            obs, notes = pol.extract_forecasts(passages, language, publication_id=publication_id, vintage=vintage,
                                               vintage_date=announced)
            res.observations.extend(obs)
            res.meta["forecast_notes"] = notes
    elif pub_type == "financial_stability_report" and numeric:
        obs, notes = stab.extract_indicators(passages, language, publication_id=publication_id,
                                             reporting_end=reporting_end, edition_label=edition_label)
        res.observations.extend(obs)
        stress, meta = stab.extract_stress_tests(passages, language, publication_id=publication_id,
                                                 reporting_end=reporting_end, edition_label=edition_label)
        res.observations.extend(stress)
        res.meta["stability_notes"] = notes
        res.meta["stress"] = {k: v for k, v in meta.items() if k != "passages"}
        res.meta["regulatory_changes"] = stab.extract_regulatory_changes(passages, language, publication_id=publication_id)
    elif pub_type == "financial_stability_report":
        res.meta["regulatory_changes"] = stab.extract_regulatory_changes(passages, language, publication_id=publication_id)
    elif pub_type == "policy_directions":
        res.meta["statement_year"] = edition_key[:4]
        res.meta["forward_looking"] = True
    if reporting_start and reporting_end:
        pub["reporting_period_start"] = reporting_start.isoformat()
        pub["reporting_period_end"] = reporting_end.isoformat()
    res.publication = pub
    res.meta.update({"pages": ex.page_count, "passages": len(passages), "language": language,
                     "extraction_method": ex.method, "pages_without_text": ex.pages_without_text,
                     "ocr_used": ex.ocr_used})
    return res


def _decision_record(row: pol.DecisionRow, publication_id: str, doc_id: str) -> dict[str, Any]:
    return {
        "decision_id": f"decision:{row.date.isoformat()}",
        "publication_id": publication_id,
        "announcement_date": row.date.isoformat(),
        "policy_rate": row.rate, "corridor_floor": row.floor, "corridor_ceiling": row.ceiling,
        "rate_change_bp": row.change_bp("rate"), "floor_change_bp": row.change_bp("floor"),
        "ceiling_change_bp": row.change_bp("ceiling"),
        "action": ("cut" if (row.change_bp("rate") or 0) < 0 else "raise" if (row.change_bp("rate") or 0) > 0 else "hold"),
        "rate_source_doc_id": doc_id, "validation_status": "verified",
    }


def _as_date(value: Any) -> dt.date | None:
    if isinstance(value, dt.date):
        return value
    if isinstance(value, str) and value:
        try:
            return dt.date.fromisoformat(value)
        except ValueError:
            return None
    return None


def _vintage_date(vintage: str) -> dt.date | None:
    try:
        year, mon = int(vintage[:4]), int(vintage[5:7])
        from ..util.periods import month_end
        return month_end(year, mon)
    except (ValueError, IndexError):
        return None
