"""Grounding validation: every number in narrative text must match an approved fact-pack number."""
from __future__ import annotations

import datetime as dt
import re
from typing import Any

from .contract import CLASSIFICATIONS, SLIDE_IDS

NUM_RE = re.compile(r"(?<![\w.])[+−-]?\d{1,3}(?:[, ]\d{3})*(?:\.\d+)?(?![\w.])")
YEAR_RE = re.compile(r"^(19|20)\d{2}$")
ORDINAL_SMALL = {"1", "2", "3", "4", "5", "6", "7", "8", "9", "10", "12", "36"}  # counts and windows are not data claims


def _numbers_in(text: str) -> list[float]:
    out = []
    for m in NUM_RE.finditer(text):
        tok = m.group(0)
        if YEAR_RE.match(tok.strip("+-")):
            continue
        if tok in ORDINAL_SMALL:
            continue
        try:
            out.append(float(tok.replace(" ", "").replace(",", "").replace("−", "-")))
        except ValueError:
            pass
    return out


def _matches(value: float, approved: list[float], text_decimals: int) -> bool:
    for a in approved:
        # accept the displayed rounding at any precision up to 2 decimals, also thousands-rounded money
        for dec in (0, 1, 2):
            if round(a, dec) == round(value, dec) and abs(a - value) <= 0.5 * 10 ** (-dec) + 1e-9:
                return True
        if abs(a) >= 1000 and abs(round(a / 1000.0, 1) - value) < 1e-9:
            return True
        if abs(a) >= 1000 and abs(round(a / 1000.0, 2) - value) < 1e-9:
            return True
        if abs(a - value) <= 0.051 and abs(a) < 1000:  # one-decimal rounding tolerance
            return True
        if abs(abs(a) - abs(value)) <= 0.051 and abs(a) < 1000:  # sign written in words ("down 4.3%")
            return True
        for dec in (0, 1):
            if round(abs(a), dec) == round(abs(value), dec) and abs(abs(a) - abs(value)) <= 0.5 * 10 ** (-dec) + 1e-9:
                return True
    return False


def validate_narrative(nar: dict[str, Any], fp: dict[str, Any]) -> dict[str, Any]:
    approved = [n["value"] for n in fp.get("approved_numbers", []) if isinstance(n.get("value"), (int, float))]
    approved += [n["value"] / 1000.0 for n in fp.get("approved_numbers", []) if isinstance(n.get("value"), (int, float)) and abs(n["value"]) >= 1000]
    # also raw chart points and table values already in the fact pack slides
    for sid, sl in fp.get("slides", {}).items():
        for k, v in (sl or {}).items():
            if isinstance(v, list):
                for item in v:
                    if isinstance(item, dict) and "points" in item:
                        approved += [p[1] for p in item["points"] if p[1] is not None]
    metric_refs = set(fp.get("metrics", {}).keys())
    approved_refs = {n.get("ref") for n in fp.get("approved_numbers", []) if n.get("ref")}
    approved_prefixes = {r.rsplit(".", 1)[0] for r in approved_refs if "." in r}
    problems: list[dict[str, Any]] = []
    checked = 0

    def check_text(where: str, text: str) -> bool:
        nonlocal checked
        ok = True
        for v in _numbers_in(text or ""):
            checked += 1
            if not _matches(v, approved, 1):
                problems.append({"where": where, "number": v, "text": (text or "")[:160], "issue": "number not in approved fact pack values"})
                ok = False
        return ok

    if nar.get("fact_pack_hash") and fp.get("fact_pack_hash") and nar["fact_pack_hash"] != fp["fact_pack_hash"]:
        problems.append({"where": "fact_pack_hash", "issue": f"narrative was written for fact pack {nar['fact_pack_hash']}, current is {fp['fact_pack_hash']}"})
    rejected_findings = []
    for f in nar.get("findings", []):
        ok = True
        if f.get("classification") not in CLASSIFICATIONS:
            problems.append({"where": f"finding {f.get('id')}", "issue": f"bad classification {f.get('classification')}"})
            ok = False
        if f.get("slide_id") not in SLIDE_IDS:
            problems.append({"where": f"finding {f.get('id')}", "issue": f"unknown slide {f.get('slide_id')}"})
            ok = False
        for ref in f.get("metric_refs", []) or []:
            if ref not in metric_refs and ref not in approved_refs and ref not in approved_prefixes and not ref.startswith(("region.", "bridge.", "m09.", "scorecard.")):
                problems.append({"where": f"finding {f.get('id')}", "issue": f"unknown metric ref {ref}"})
                ok = False
        if f.get("period"):
            try:
                dt.date.fromisoformat(f["period"])
            except ValueError:
                problems.append({"where": f"finding {f.get('id')}", "issue": "period is not a date"})
                ok = False
        if not check_text(f"finding {f.get('id')}", f.get("statement", "")):
            ok = False
        check_text(f"finding {f.get('id')} caveat", f.get("caveat", ""))
        if not ok:
            rejected_findings.append(f.get("id"))
    rejected_slides = []
    for sid, s in (nar.get("slides") or {}).items():
        ok = check_text(f"{sid} title", s.get("title", ""))
        for i, t in enumerate(s.get("interpretations") or []):
            ok = check_text(f"{sid} interpretation {i+1}", t) and ok
        ok = check_text(f"{sid} so_what", s.get("so_what", "")) and ok
        ok = check_text(f"{sid} caveat", s.get("caveat", "")) and ok
        if not ok:
            rejected_slides.append(sid)
    for i, q in enumerate(nar.get("questions") or []):
        for k in ("question", "signal", "why", "watch"):
            check_text(f"question {i+1} {k}", q.get(k, ""))
    check_text("cover", (nar.get("cover") or {}).get("headline", ""))
    result = {"numbers_checked": checked, "problems": problems, "rejected_findings": rejected_findings, "rejected_slides": rejected_slides,
              "ok": not problems}
    return result


def apply_fallback(nar: dict[str, Any], fallback: dict[str, Any], validation: dict[str, Any]) -> dict[str, Any]:
    """Replace rejected slide texts / findings with the facts-only equivalents; keep the rest."""
    out = dict(nar)
    out["slides"] = dict(nar.get("slides") or {})
    for sid in validation.get("rejected_slides", []):
        out["slides"][sid] = dict(fallback["slides"].get(sid, {}))
        out["slides"][sid]["fallback"] = True
    if validation.get("rejected_findings"):
        keep = [f for f in nar.get("findings", []) if f.get("id") not in validation["rejected_findings"]]
        out["findings"] = keep if keep else fallback["findings"]
    if any(p.get("where") == "fact_pack_hash" for p in validation.get("problems", [])):
        out["stale_fact_pack"] = True
    out["validation"] = validation
    return out
