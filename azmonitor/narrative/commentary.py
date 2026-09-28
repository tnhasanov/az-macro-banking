"""Support for writing fresh commentary in a Claude Code session, without a paid API.

The deterministic half of the work is done by the tool: refresh the sources, build the fact pack,
and write a *commentary request* — the numbers that may be cited, each with the claim id that grounds
it, the passages that may be quoted, and the rules the text has to satisfy. A Claude Code session
reads that file, writes the narrative JSON, and the validator checks every number and quotation
before anything is rendered. No model API call is involved anywhere in that path.

`bind_claims` is the authoring aid: given a narrative whose prose is already written, it proposes the
claim id for each number and reports the ones that are ambiguous or unsupported, so the author
resolves those rather than hand-writing hundreds of ids. Proposals are checked by the validator like
any other claim, so a wrong proposal fails the report rather than passing silently.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from . import claims as C
from .contract import CLASSIFICATIONS, SLIDE_IDS, blocks_in, claims_of, literals_of, text_of

RULES = [
    "Every number in the text must be covered by a claim id listed in `claim_catalogue`. A number "
    "that is not a measurement (a count of items, a slide number) goes in `literals` with a reason.",
    "A claim binds the metric, its dimensions, the period, the unit and the comparison basis. Writing "
    "a year-on-year rate as a level, or a percentage-point move as a percentage, is rejected.",
    "Rounding must match: write 12.9% for 12.88%, never 13%. A direction word next to a number is "
    "checked against the sign of the claim.",
    "A statement attributed to the Central Bank must quote a passage from `quotable_passages` and be "
    "classified `cba_assessment` (its view) or `cba_forecast` (its projection).",
    "Classifications: " + ", ".join(sorted(CLASSIFICATIONS)) + ".",
    "Stress-test results are projections under a scenario, never forecasts and never outcomes. "
    "Central Bank projections are not our scenarios and not outcomes.",
    "Do not assert anything about this bank's own exposures, vulnerabilities or compliance: none of "
    "these sources contains bank-level data. Implications are conditional and are labelled as such.",
    "Where the evidence does not support a statement, say what is missing instead of writing it.",
]


def build_request(fp: dict[str, Any], passages: dict[str, Any], max_passages: int = 80) -> dict[str, Any]:
    """The pack a commentary session works from.

    Quotable passages are drawn from the publications this edition is about — the current decision,
    review and stability report — because those are the ones a commentary quotes. Older editions stay
    in the database and can be cited by passage id when a comparison needs them.
    """
    catalogue = C.catalogue(fp)
    slides: dict[str, Any] = {}
    for sid in [s for s in SLIDE_IDS if s in (fp.get("slides") or {})]:
        entry = fp["slides"][sid] or {}
        refs = sorted({c["claim_id"] for c in catalogue if _mentions(entry, c["claim_id"].split("@")[0].split("|")[0])})
        slides[sid] = {"claim_ids": refs[:40], "keys": sorted(k for k in entry.keys() if k != "note"),
                       "note": entry.get("note")}
    quotable = []
    seen_text: set[str] = set()
    for pid, p in list(passages.items()):
        text = (p.get("text") or "").strip()
        if len(text) < 120 or text.count(".....") or text.count(" – ") > 6:
            continue                       # too short, a table of contents, or an acronym list
        key = text[:120].lower()
        if key in seen_text:
            continue                       # the two-column reading can produce near-duplicates
        seen_text.add(key)
        quotable.append({"passage_id": pid, "cite": p.get("cite"), "language": p.get("language"),
                         "publication_id": p.get("publication_id"), "text": text[:600]})
    pub = fp.get("publications") or {}
    current_ids = [x for x in [
        (((pub.get("policy") or {}).get("decision") or {}).get("publication") or {}).get("publication_id"),
        ((pub.get("policy") or {}).get("review") or {}).get("publication_id"),
        ((pub.get("policy") or {}).get("previous_review") or {}).get("publication_id"),
        ((pub.get("stability") or {}).get("report") or {}).get("publication_id"),
    ] if x]
    def page_no(row: dict[str, Any]) -> int:
        token = str(row.get("cite") or "").split()
        return int(token[1]) if len(token) > 1 and token[1].isdigit() else 0

    quotable.sort(key=lambda r: (r["publication_id"] not in current_ids, (r.get("language") or "") != "en",
                                 r["publication_id"] or "", page_no(r)))
    # a quota per publication, so the decision and the review are not crowded out by the longest report
    per_pub = max(6, max_passages // max(1, len(current_ids) or 1))
    balanced, counts = [], {}
    for row in quotable:
        pid = row["publication_id"]
        if counts.get(pid, 0) >= per_pub:
            continue
        counts[pid] = counts.get(pid, 0) + 1
        balanced.append(row)
    quotable = balanced
    return {
        "report_type": fp.get("report_type"), "as_of": fp.get("as_of"), "edition": fp.get("edition"),
        "fact_pack_hash": fp.get("fact_pack_hash"),
        "instructions": {
            "goal": "Write the narrative JSON for this edition: a cover headline, up to five ranked findings, "
                    "slide texts and management questions.",
            "output_path": "narratives/<report>_<edition>_analyst.json",
            "must_set": {"fact_pack_hash": fp.get("fact_pack_hash"), "mode": "analyst_file"},
            "block_shape": {"text": "...", "claims": ["<claim id>"], "literals": [{"value": 3, "reason": "..."}],
                            "quotes": [{"passage_id": "...", "text": "quoted words"}]},
            "rules": RULES,
            "validate_with": "python -m azmonitor.cli report --type monthly --narrative-file <path>",
            "bind_helper": "python -m azmonitor.cli bind-claims --narrative <path> (proposes claim ids, reports "
                           "ambiguous and unsupported numbers)",
        },
        "reporting_periods": fp.get("edition"),
        "anchors": fp.get("anchors"),
        "availability": (fp.get("availability") or {}).get("missing"),
        "quality": (fp.get("quality") or {}).get("summary"),
        "policy": {"stance": ((pub.get("policy") or {}).get("stance")),
                   "decision": ((pub.get("policy") or {}).get("decision") or {}).get("announcement_date"),
                   "forecast_vintage": ((pub.get("policy") or {}).get("forecasts") or {}).get("current_vintage")},
        "stability": {"report": ((pub.get("stability") or {}).get("report") or {}).get("edition"),
                      "reporting_period_end": ((pub.get("stability") or {}).get("report") or {}).get("reporting_period_end")},
        "slides": slides,
        "claim_catalogue": catalogue,
        "quotable_passages": quotable[:max_passages],
    }


def _mentions(entry: Any, metric_ref: str) -> bool:
    return metric_ref in json.dumps(entry, default=str, ensure_ascii=False)


def bind_claims(narrative: dict[str, Any], fp: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Propose a claim id for every number in a narrative.

    Unambiguous matches are written into the block; ambiguous and unsupported numbers are listed for
    the author. Nothing is guessed: where two claims share a value, the author picks.
    """
    catalogue = C.catalogue(fp)
    resolved = [(c, C.resolve(c["claim_id"], fp)) for c in catalogue]
    report: dict[str, Any] = {"bound": 0, "ambiguous": [], "unsupported": [], "blocks": 0}
    # Metrics a slide actually shows: used to choose between claims that happen to share a value.
    slide_metrics: dict[str, set[str]] = {}
    for sid, entry in (fp.get("slides") or {}).items():
        blob = json.dumps(entry, default=str, ensure_ascii=False)
        slide_metrics[sid] = {c["claim_id"] for c in catalogue if c["claim_id"].split("@")[0].split("|")[0] in blob}

    def bind_block(where: str, b: Any, prefer: set[str] | None = None) -> Any:
        if isinstance(b, str):
            b = {"text": b}
        text = text_of(b)
        numbers = C.numbers_in(text)
        if not numbers:
            return b.get("text") if set(b.keys()) == {"text"} else b
        report["blocks"] += 1
        existing = list(claims_of(b))
        literals = {round(float(l["value"]), 6) for l in literals_of(b) if isinstance(l.get("value"), (int, float))}
        chosen: list[str] = list(existing)
        for n in numbers:
            if round(n.value, 6) in literals:
                continue
            if any(C.matches_value(n, r) and C.unit_consistent(n, r)
                   for cid in existing for c, r in resolved if c["claim_id"] == cid):
                continue
            candidates = [(c, r) for c, r in resolved if C.matches_value(n, r) and C.unit_consistent(n, r)]
            if not candidates:
                report["unsupported"].append({"where": where, "written": n.written, "value": n.value,
                                              "text": text[:140]})
                continue
            exact = [(c, r) for c, r in candidates if abs((r.value or 0) - n.value) < 1e-9]
            pick = exact if len(exact) == 1 else candidates
            if prefer and len(pick) > 1:
                preferred = [(c, r) for c, r in pick if c["claim_id"] in prefer]
                if preferred:
                    pick = preferred
            unique_ids = list(dict.fromkeys(c["claim_id"] for c, _ in pick))
            if len(unique_ids) == 1:
                pick = pick[:1]
            if len(pick) == 1:
                chosen.append(pick[0][0]["claim_id"])
                report["bound"] += 1
            else:
                report["ambiguous"].append({"where": where, "written": n.written, "text": text[:140],
                                            "options": unique_ids[:6]})
        out = dict(b)
        if chosen:
            out["claims"] = list(dict.fromkeys(chosen))
        return out

    nar = json.loads(json.dumps(narrative))
    nar["cover"] = dict(nar.get("cover") or {})
    if nar["cover"].get("headline"):
        nar["cover"]["headline"] = bind_block("cover headline", nar["cover"]["headline"])
    for f in nar.get("findings") or []:
        prefer = {c["claim_id"] for c in catalogue
                  if c["claim_id"].split("@")[0].split("|")[0] in (f.get("metric_refs") or [])}
        prefer |= slide_metrics.get(f.get("slide_id") or "", set())
        for key in ("statement", "banking_relevance", "caveat"):
            if f.get(key):
                f[key] = bind_block(f"finding {f.get('id')} {key}", f[key], prefer)
    for sid, s in (nar.get("slides") or {}).items():
        prefer = slide_metrics.get(sid, set())
        for key in ("title", "so_what", "caveat"):
            if (s or {}).get(key):
                s[key] = bind_block(f"{sid} {key}", s[key], prefer)
        if (s or {}).get("interpretations"):
            s["interpretations"] = [bind_block(f"{sid} interpretation {i+1}", t, prefer)
                                    for i, t in enumerate(s["interpretations"])]
    for i, q in enumerate(nar.get("questions") or [], start=1):
        for key in ("question", "signal", "why", "internal_data", "watch"):
            if q.get(key):
                q[key] = bind_block(f"question {i} {key}", q[key])
    nar["fact_pack_hash"] = fp.get("fact_pack_hash")
    return nar, report


def write_request(fp: dict[str, Any], passages: dict[str, Any], out_path: Path) -> Path:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(build_request(fp, passages), ensure_ascii=False, indent=1, default=str),
                        encoding="utf-8")
    return out_path
