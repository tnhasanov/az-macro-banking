"""Grounding validation.

Three things are checked, in this order:

1. **The narrative belongs to this fact pack.** A narrative written against an earlier fact pack is
   rejected outright rather than partially reused, because its numbers describe superseded data.
2. **Every number is bound to one claim** naming metric, dimensions, period, unit and comparison
   basis; the claim is re-resolved against the fact pack and checked on value, rounding, unit,
   period and the direction the sentence asserts. No number is exempt for being small: a 0.4 pp
   move in the overdue ratio is a financial claim. Numbers that are not measurements have to be
   declared as literals with a reason.
3. **Every statement attributed to a source is quoted from it.** A finding classified as the Central
   Bank's assessment or projection must cite a passage, and the quoted words must actually appear in
   that passage of that document.

Whatever fails is reported per item and falls back to the facts-only text for that item.
"""
from __future__ import annotations

import datetime as dt
import re
from typing import Any

from . import claims as C
from .contract import (CLASSIFICATIONS, SLIDE_IDS, SOURCE_ATTRIBUTED, claims_of, literals_of, quotes_of,
                       text_of)


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9əğıöşüçİ%.,+-]+", " ", (text or "").lower()).strip()


def _check_quotes(where: str, b: Any, passages: dict[str, Any], problems: list[dict[str, Any]]) -> bool:
    ok = True
    for q in quotes_of(b):
        pid = q.get("passage_id")
        p = passages.get(pid) if pid else None
        if p is None:
            problems.append({"where": where, "issue": f"quoted passage {pid!r} is not in the evidence store",
                             "kind": "quote"})
            ok = False
            continue
        quoted = _norm(q.get("text") or "")
        if not quoted:
            problems.append({"where": where, "issue": "a quote was declared without any quoted words", "kind": "quote"})
            ok = False
            continue
        if quoted not in _norm(p.get("text") or ""):
            problems.append({"where": where, "kind": "quote",
                             "issue": f"quoted words are not in passage {pid} ({p.get('cite') or ''}): {q.get('text')[:90]!r}"})
            ok = False
    return ok


def _check_block(where: str, b: Any, fp: dict[str, Any], passages: dict[str, Any],
                 problems: list[dict[str, Any]]) -> tuple[bool, int]:
    """Bind every number in one block. Returns (ok, numbers_checked)."""
    text = text_of(b)
    numbers = C.numbers_in(text)
    declared = claims_of(b)
    literals = {round(float(l["value"]), 6): l for l in literals_of(b) if isinstance(l.get("value"), (int, float))}
    ok = _check_quotes(where, b, passages, problems)
    resolved: list[C.ResolvedClaim] = []
    for cid in declared:
        r = C.resolve(cid, fp)
        if r.problem:
            problems.append({"where": where, "claim": cid, "issue": r.problem, "kind": "claim"})
            ok = False
        else:
            resolved.append(r)
    for n in numbers:
        if round(n.value, 6) in literals:
            continue
        candidates = [r for r in resolved if C.matches_value(n, r)]
        if not candidates:
            problems.append({"where": where, "number": n.value, "written": n.written, "kind": "unbound",
                             "issue": f"{n.written} is not covered by any declared claim for this text",
                             "text": text[:160]})
            ok = False
            continue
        unit_ok = [r for r in candidates if C.unit_consistent(n, r)]
        if not unit_ok:
            problems.append({"where": where, "number": n.value, "written": n.written, "kind": "unit",
                             "issue": f"{n.written} is written with a unit the claim does not carry "
                                      f"({', '.join(str(r.unit) for r in candidates)})", "text": text[:160]})
            ok = False
            continue
        direction_problems = [C.direction_ok(text, n, r) for r in unit_ok]
        if direction_problems and all(d is not None for d in direction_problems):
            problems.append({"where": where, "number": n.value, "written": n.written, "kind": "direction",
                             "issue": direction_problems[0], "text": text[:160]})
            ok = False
    return ok, len(numbers)


def validate_narrative(nar: dict[str, Any], fp: dict[str, Any], passages: dict[str, Any] | None = None) -> dict[str, Any]:
    passages = passages or {}
    problems: list[dict[str, Any]] = []
    checked = 0
    metric_refs = set(fp.get("metrics", {}).keys())

    stale = bool(nar.get("fact_pack_hash") and fp.get("fact_pack_hash") and nar["fact_pack_hash"] != fp["fact_pack_hash"])
    missing_hash = not nar.get("fact_pack_hash")
    if stale:
        problems.append({"where": "fact_pack_hash", "kind": "stale",
                         "issue": f"narrative was written against fact pack {nar['fact_pack_hash']}, "
                                  f"this report is built from {fp.get('fact_pack_hash')}"})
    if missing_hash:
        problems.append({"where": "fact_pack_hash", "kind": "stale",
                         "issue": "narrative does not state which fact pack it was written against"})
    if stale or missing_hash:
        return {"numbers_checked": 0, "problems": problems, "rejected_findings": [f.get("id") for f in nar.get("findings", [])],
                "rejected_slides": sorted((nar.get("slides") or {}).keys()), "stale": True, "ok": False,
                "note": "the whole narrative was rejected because it does not match this fact pack"}

    rejected_findings: list[str] = []
    for f in nar.get("findings", []):
        fid = f.get("id")
        ok = True
        if f.get("classification") not in CLASSIFICATIONS:
            problems.append({"where": f"finding {fid}", "kind": "classification",
                             "issue": f"classification {f.get('classification')!r} is not one of {sorted(CLASSIFICATIONS)}"})
            ok = False
        if f.get("slide_id") not in SLIDE_IDS:
            problems.append({"where": f"finding {fid}", "kind": "slide", "issue": f"unknown slide {f.get('slide_id')}"})
            ok = False
        for ref in f.get("metric_refs", []) or []:
            base = ref.split("|")[0]
            if ref not in metric_refs and base not in {m.split("|")[0] for m in metric_refs}:
                problems.append({"where": f"finding {fid}", "kind": "metric", "issue": f"unknown metric ref {ref}"})
                ok = False
        if f.get("period"):
            try:
                dt.date.fromisoformat(f["period"])
            except ValueError:
                problems.append({"where": f"finding {fid}", "kind": "period", "issue": "period is not a date"})
                ok = False
        if f.get("classification") in SOURCE_ATTRIBUTED and not any(quotes_of(f.get(k)) for k in
                                                                    ("statement", "banking_relevance", "caveat")):
            problems.append({"where": f"finding {fid}", "kind": "quote",
                             "issue": f"a finding classified {f.get('classification')} must quote the passage it "
                                      f"attributes the statement to"})
            ok = False
        for key in ("statement", "banking_relevance", "caveat"):
            if f.get(key):
                block_ok, n = _check_block(f"finding {fid} {key}", f[key], fp, passages, problems)
                checked += n
                ok = ok and (block_ok or key == "caveat")
        if not ok:
            rejected_findings.append(fid)

    rejected_slides: list[str] = []
    for sid, s in (nar.get("slides") or {}).items():
        ok = True
        if sid not in SLIDE_IDS:
            problems.append({"where": sid, "kind": "slide", "issue": f"unknown slide id {sid}"})
            ok = False
        for key in ("title", "so_what", "caveat"):
            if (s or {}).get(key):
                block_ok, n = _check_block(f"{sid} {key}", s[key], fp, passages, problems)
                checked += n
                ok = ok and block_ok
        for i, t in enumerate((s or {}).get("interpretations") or [], start=1):
            block_ok, n = _check_block(f"{sid} interpretation {i}", t, fp, passages, problems)
            checked += n
            ok = ok and block_ok
        if not ok:
            rejected_slides.append(sid)

    for i, q in enumerate(nar.get("questions") or [], start=1):
        for key in ("question", "signal", "why", "internal_data", "watch"):
            if q.get(key):
                _, n = _check_block(f"question {i} {key}", q[key], fp, passages, problems)
                checked += n
    cover = (nar.get("cover") or {}).get("headline")
    if cover:
        _, n = _check_block("cover headline", cover, fp, passages, problems)
        checked += n

    return {"numbers_checked": checked, "problems": problems, "rejected_findings": rejected_findings,
            "rejected_slides": rejected_slides, "stale": False, "ok": not problems,
            "counts": _count_kinds(problems)}


def _count_kinds(problems: list[dict[str, Any]]) -> dict[str, int]:
    out: dict[str, int] = {}
    for p in problems:
        out[p.get("kind", "other")] = out.get(p.get("kind", "other"), 0) + 1
    return out


def apply_fallback(nar: dict[str, Any], fallback: dict[str, Any], validation: dict[str, Any]) -> dict[str, Any]:
    """Replace rejected slide texts and findings with the facts-only equivalents; keep the rest.

    A stale narrative is replaced wholesale, and the report records that it fell back.
    """
    if validation.get("stale"):
        out = dict(fallback)
        out["validation"] = validation
        out["stale_fact_pack"] = True
        out["fallback_scope"] = "whole narrative"
        return out
    out = dict(nar)
    out["slides"] = dict(nar.get("slides") or {})
    for sid in validation.get("rejected_slides", []):
        out["slides"][sid] = dict(fallback.get("slides", {}).get(sid, {}))
        out["slides"][sid]["fallback"] = True
    if validation.get("rejected_findings"):
        keep = [f for f in nar.get("findings", []) if f.get("id") not in validation["rejected_findings"]]
        out["findings"] = keep if keep else fallback.get("findings", [])
        out["fallback_scope"] = f"{len(validation['rejected_findings'])} finding(s), {len(validation.get('rejected_slides', []))} slide(s)"
    out["validation"] = validation
    return out
