"""Structured narrative input/output contract.

A narrative is written by the facts-only generator, by an analyst (or a Claude Code commentary
routine) as a JSON file, or by the optional API provider. Every piece of prose is a *block*: either
a plain string, or an object that binds the numbers and quotations in that prose to the evidence
behind them.

    {"text": "Loans to the economy grew 12.9% y/y in end-Jul 2026.",
     "claims": ["cba.loans.total_ci.yoy@2026-07-31#yoy"],
     "literals": [{"value": 5, "reason": "count of findings on this slide"}],
     "quotes": [{"passage_id": "...", "text": "inflation remains within the target range"}]}

`claims` grounds every number (see narrative/claims.py). `literals` covers the few numbers that are
not measurements, each with a stated reason. `quotes` grounds a statement attributed to a source: the
quoted words must appear in the cited passage of the cited document.

Narrative JSON:
{
  "report_type": "monthly", "as_of": "YYYY-MM-DD", "lang": "en", "mode": "facts_only|analyst_file|api",
  "fact_pack_hash": "...",                       # must match the fact pack being rendered
  "cover": {"headline": <block>},
  "findings": [
    {"id": "F1", "rank": 1, "slide_id": "M08",
     "classification": "observed_fact|cba_assessment|cba_forecast|interpretation|hypothesis|management_question",
     "statement": <block>, "metric_refs": [...], "period": "2026-07-31", "comparison": "y/y",
     "banking_relevance": <block>, "caveat": <block>, "direction": "adverse|favourable|neutral",
     "status": "new|revision|continuing"}
  ],
  "slides": {"M04": {"title": <block>, "interpretations": [<block>, ...], "so_what": <block>, "caveat": <block>}},
  "questions": [{"question": <block>, "signal": <block>, "why": <block>, "internal_data": "...",
                 "watch": "...", "function": "Treasury / ALM"}],
  "validation": {...}                            # filled by validate.py
}
"""
from __future__ import annotations

from typing import Any

# What kind of statement a finding is. The four evidence classes are kept apart on purpose: the
# Central Bank's own assessment, its published projection, an observed outcome and our reading of
# the evidence carry different weight for a management decision.
CLASSIFICATIONS = {"observed_fact", "cba_assessment", "cba_forecast", "interpretation", "hypothesis",
                   "management_question"}
SOURCE_ATTRIBUTED = {"cba_assessment", "cba_forecast"}

SLIDE_IDS = [f"M{i:02d}" for i in range(1, 23)] + ["A01", "A02", "A03", "A04", "A05", "A06"] + \
            [f"P{i:02d}" for i in range(1, 8)] + [f"S{i:02d}" for i in range(1, 8)] + \
            [f"D{i:02d}" for i in range(1, 4)]


def block(text: str, claims: list[str] | None = None, literals: list[dict[str, Any]] | None = None,
          quotes: list[dict[str, Any]] | None = None) -> dict[str, Any] | str:
    """Build a block, staying a plain string when there is nothing to ground."""
    if not (claims or literals or quotes):
        return text
    out: dict[str, Any] = {"text": text}
    if claims:
        out["claims"] = list(dict.fromkeys(claims))
    if literals:
        out["literals"] = literals
    if quotes:
        out["quotes"] = quotes
    return out


def text_of(b: Any) -> str:
    if isinstance(b, dict):
        return b.get("text") or ""
    return b or ""


def claims_of(b: Any) -> list[str]:
    return list((b or {}).get("claims") or []) if isinstance(b, dict) else []


def literals_of(b: Any) -> list[dict[str, Any]]:
    return list((b or {}).get("literals") or []) if isinstance(b, dict) else []


def quotes_of(b: Any) -> list[dict[str, Any]]:
    return list((b or {}).get("quotes") or []) if isinstance(b, dict) else []


def empty_narrative(fp: dict[str, Any], mode: str) -> dict[str, Any]:
    return {"report_type": fp.get("report_type", "monthly"), "as_of": fp.get("as_of"), "lang": fp.get("lang", "en"),
            "mode": mode, "fact_pack_hash": fp.get("fact_pack_hash"), "cover": {"headline": ""}, "findings": [],
            "slides": {}, "questions": [], "validation": {}}


def slide_text(nar: dict[str, Any], slide_id: str) -> dict[str, Any]:
    s = nar.get("slides", {}).get(slide_id, {}) or {}
    return {"title": text_of(s.get("title")), "interpretations": [text_of(t) for t in (s.get("interpretations") or [])],
            "so_what": text_of(s.get("so_what")), "caveat": text_of(s.get("caveat"))}


def flatten(nar: dict[str, Any]) -> dict[str, Any]:
    """A copy with every block reduced to its text, for rendering."""
    out = dict(nar)
    out["cover"] = {**(nar.get("cover") or {}), "headline": text_of((nar.get("cover") or {}).get("headline"))}
    out["findings"] = []
    for f in nar.get("findings") or []:
        g = dict(f)
        for key in ("statement", "banking_relevance", "caveat"):
            if key in g:
                g[key] = text_of(g[key])
        out["findings"].append(g)
    out["slides"] = {}
    for sid, s in (nar.get("slides") or {}).items():
        t = dict(s or {})
        t["title"] = text_of(t.get("title"))
        t["interpretations"] = [text_of(x) for x in (t.get("interpretations") or [])]
        t["so_what"] = text_of(t.get("so_what"))
        t["caveat"] = text_of(t.get("caveat"))
        out["slides"][sid] = t
    out["questions"] = []
    for q in nar.get("questions") or []:
        g = dict(q)
        for key in ("question", "signal", "why", "internal_data", "watch"):
            if key in g:
                g[key] = text_of(g[key])
        out["questions"].append(g)
    return out


def blocks_in(nar: dict[str, Any]):
    """Every block in a narrative, with a label for error messages."""
    yield "cover headline", (nar.get("cover") or {}).get("headline")
    for f in nar.get("findings") or []:
        fid = f.get("id") or "finding"
        for key in ("statement", "banking_relevance", "caveat"):
            if f.get(key):
                yield f"finding {fid} {key}", f[key]
    for sid, s in (nar.get("slides") or {}).items():
        for key in ("title", "so_what", "caveat"):
            if (s or {}).get(key):
                yield f"{sid} {key}", s[key]
        for i, t in enumerate((s or {}).get("interpretations") or [], start=1):
            yield f"{sid} interpretation {i}", t
    for i, q in enumerate(nar.get("questions") or [], start=1):
        for key in ("question", "signal", "why", "internal_data", "watch"):
            if q.get(key):
                yield f"question {i} {key}", q[key]
