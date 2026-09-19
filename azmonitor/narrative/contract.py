"""Structured narrative input/output contract.

Narrative JSON (produced by the facts-only generator, an analyst / Claude Code session, or an API call):
{
  "report_type": "monthly", "as_of": "YYYY-MM-DD", "lang": "en", "mode": "facts_only|analyst_file|api",
  "fact_pack_hash": "...",                       # must match the fact pack used for rendering
  "cover": {"headline": "..."},
  "findings": [                                  # up to five, ranked
    {"id": "F1", "rank": 1, "slide_id": "M08", "classification": "observed_fact|interpretation|hypothesis|management_question",
     "statement": "...", "metric_refs": ["cba.loans.total_ci.yoy"], "period": "2026-07-31", "comparison": "y/y",
     "banking_relevance": "...", "caveat": "...", "direction": "adverse|favourable|neutral", "status": "new|revision|continuing"}
  ],
  "slides": {"M04": {"title": "...", "interpretations": ["...", "..."], "so_what": "...", "caveat": "..."}, ...},
  "questions": [{"question": "...", "signal": "...", "why": "...", "internal_data": "...", "watch": "...", "function": "Treasury/ALM"}],
  "validation": {...}                            # filled by validate.py
}
"""
from __future__ import annotations

from typing import Any

CLASSIFICATIONS = {"observed_fact", "interpretation", "hypothesis", "management_question"}
SLIDE_IDS = [f"M{i:02d}" for i in range(1, 19)] + ["A01", "A02", "A03", "A04"]


def empty_narrative(fp: dict[str, Any], mode: str) -> dict[str, Any]:
    return {"report_type": fp.get("report_type", "monthly"), "as_of": fp.get("as_of"), "lang": fp.get("lang", "en"), "mode": mode,
            "fact_pack_hash": fp.get("fact_pack_hash"), "cover": {"headline": ""}, "findings": [], "slides": {}, "questions": [], "validation": {}}


def slide_text(nar: dict[str, Any], slide_id: str) -> dict[str, Any]:
    s = nar.get("slides", {}).get(slide_id, {}) or {}
    return {"title": s.get("title") or "", "interpretations": list(s.get("interpretations") or []), "so_what": s.get("so_what") or "",
            "caveat": s.get("caveat") or ""}
