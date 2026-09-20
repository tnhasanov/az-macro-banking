"""Policy and stability facts for the report: what the Central Bank decided, projected and assessed.

Three rules shape everything here.

*Two dates, always.* A publication's reporting period and its release date are different facts and
both are carried to the slide. A stability indicator from the 2025 report is the latest available
measure of regulatory capital even in a July 2026 deck, and it is labelled with both dates rather
than being silently treated as current.

*Native frequency, never carried forward.* An annual or half-yearly observation stays annual or
half-yearly. Nothing here creates a monthly point by repeating an older figure.

*Cutoff by release, not by period.* An as-of report excludes publications released after the cutoff,
so a reconstruction of July does not quote an August report.
"""
from __future__ import annotations

import datetime as dt
import json
from typing import Any

from ..storage.db import Database
from ..util.log import get_logger
from . import policy as pol

log = get_logger("pub_facts")

PUB_LABELS = {
    "monetary_policy_review": "Monetary Policy Review",
    "policy_decision": "Monetary policy decision",
    "policy_directions": "Annual monetary policy statement",
    "financial_stability_report": "Financial Stability Report",
}
# Where the same concept is published by more than one source, the statistical table wins for the
# months it covers and the report is kept as a cross-reference. Definitions differ, so the pair is
# reported side by side rather than merged.
SOURCE_PRIORITY = [
    {"concept": "NPL ratio", "primary": "cba.bank.npl.ratio", "secondary": "cba.fsr.npl_ratio",
     "note": "the monthly prudential table and the stability report both publish a non-performing loan ratio; "
             "coverage and timing differ, so both are shown with their own reporting dates"},
    {"concept": "Deposit dollarisation", "primary": "cba.deposits.fx_share", "secondary": "cba.fsr.deposit_dollarisation",
     "note": "the monthly table measures the foreign-currency share of all deposits; the stability report quotes "
             "resident household deposits"},
]


def _row(o: Any) -> dict[str, Any]:
    return {k: o[k] for k in o.keys()}


def _iso(d: Any) -> str | None:
    return d.isoformat() if isinstance(d, dt.date) else (d or None)


class PublicationFacts:
    def __init__(self, db: Database, as_of: dt.date, lang: str = "en", metrics: dict[str, Any] | None = None):
        self.db = db
        self.as_of = as_of
        self.lang = lang
        self.metrics = metrics or {}     # computed metrics from the fact pack, for cross-source reconciliation
        self.cutoff = as_of.isoformat()
        self.publications = [_row(p) for p in db.publications(cutoff=self.cutoff)]
        # A publication with no verified release date cannot be placed before or after a historical
        # cutoff. Those that were first seen after the cutoff are left out and disclosed.
        all_rows = [_row(p) for p in db.publications()]
        self.excluded_unknown_date = [
            {"publication_id": p["publication_id"], "edition": p.get("edition_label"), "pub_type": p["pub_type"]}
            for p in all_rows
            if not p.get("published_at") and not p.get("announcement_date") and (p.get("first_seen_at") or "") > self.cutoff]
        self.by_type: dict[str, list[dict[str, Any]]] = {}
        for p in self.publications:
            self.by_type.setdefault(p["pub_type"], []).append(p)
        for rows in self.by_type.values():
            rows.sort(key=self._chronological)
        self.visible_ids = {p["publication_id"] for p in self.publications}

    # ------------------------------------------------------------------ helpers
    @staticmethod
    def _edition_date(edition_key: str | None) -> str:
        """A sortable date derived from the edition key, used only for ordering.

        Reviews in the current format state no reporting period, so ordering falls back to the
        edition they are named for. This is never presented as a reporting period.
        """
        key = edition_key or ""
        if len(key) == 10 and key[4] == "-" and key[7] == "-":
            return key                                   # a decision: an exact date
        if len(key) == 7 and key[4] == "-" and key[5:].isdigit():
            return f"{key}-28"                           # a monthly edition
        year = key[:4] if key[:4].isdigit() else ""
        if not year:
            return ""
        if key.endswith("H1"):
            return f"{year}-06-30"
        return f"{year}-12-31"                           # annual report or annual statement

    def _chronological(self, p: dict[str, Any]) -> tuple[str, str, str]:
        """Order editions by what they report on, then by when they appeared.

        Sorting on the edition key alone would put the half-year report after the annual one for the
        same year, because "2025-A" precedes "2025-H1" alphabetically.
        """
        return (p.get("reporting_period_end") or self._edition_date(p.get("edition_key")),
                p.get("published_at") or "", p.get("edition_key") or "")

    def latest(self, pub_type: str, back: int = 0) -> dict[str, Any] | None:
        rows = self.by_type.get(pub_type) or []
        if len(rows) <= back:
            return None
        return rows[-1 - back]

    def editions(self, pub_type: str) -> list[dict[str, Any]]:
        return list(self.by_type.get(pub_type) or [])

    def documents(self, publication_id: str) -> list[dict[str, Any]]:
        return [_row(d) for d in self.db.publication_documents(publication_id)]

    def observations(self, series_prefix: str) -> list[dict[str, Any]]:
        """Current observations extracted from publications visible at the cutoff."""
        rows = self.db.conn.execute(
            "SELECT * FROM observations WHERE status='current' AND series_id LIKE ? ORDER BY series_id, period_end",
            (series_prefix + "%",)).fetchall()
        out = []
        for r in rows:
            if r["publication_id"] and r["publication_id"] not in self.visible_ids:
                continue
            if (r["validation_status"] or "verified") != "verified":
                continue          # held for review: not used in any report until it is checked
            d = _row(r)
            d["dims"] = json.loads(d["dims"] or "{}")
            out.append(d)
        return out

    def _pub_of(self, publication_id: str | None) -> dict[str, Any] | None:
        return next((p for p in self.publications if p["publication_id"] == publication_id), None)

    def dated(self, obs: dict[str, Any]) -> dict[str, Any]:
        """An observation with both of its dates and the release it came from."""
        pub = self._pub_of(obs.get("publication_id"))
        return {
            "value": obs["value"], "unit": obs["unit"], "observation_date": obs["period_end"],
            "period_start": obs["period_start"], "period_type": obs["period_type"], "frequency": obs["freq"],
            "scenario": obs.get("scenario"), "definition": obs.get("basis"), "population": obs.get("population"),
            "validation_status": obs.get("validation_status"), "source_citation": obs.get("cell_ref"),
            "publication": {
                "id": obs.get("publication_id"),
                "label": (pub or {}).get("edition_label"),
                "type": PUB_LABELS.get((pub or {}).get("pub_type", ""), (pub or {}).get("pub_type")),
                "published_at": (pub or {}).get("published_at"),
                "published_at_basis": (pub or {}).get("published_at_basis"),
                "reporting_period_end": (pub or {}).get("reporting_period_end"),
            },
            "dims": obs.get("dims") or {},
            "passage_id": obs.get("passage_id"),
        }

    # ------------------------------------------------------------------ policy
    def decisions(self) -> list[dict[str, Any]]:
        rows = [_row(r) for r in self.db.decisions(cutoff=self.cutoff)]
        return sorted(rows, key=lambda r: r["announcement_date"])

    def policy_block(self) -> dict[str, Any]:
        decisions = self.decisions()
        current = decisions[-1] if decisions else None
        previous = decisions[-2] if len(decisions) > 1 else None
        review = self.latest("monetary_policy_review")
        prior_review = self.latest("monetary_policy_review", back=1)
        rate_series = [o for o in self.observations("cba.policy.rate")]
        corridor = {
            "floor": [o for o in self.observations("cba.policy.corridor_floor")],
            "ceiling": [o for o in self.observations("cba.policy.corridor_ceiling")],
        }
        stance = self._stance(current, previous)
        forecasts = self.forecast_block(review, prior_review)
        return {
            "decision": self._decision_view(current),
            "previous_decision": self._decision_view(previous),
            "stance": stance,
            "corridor_now": {
                "floor": current.get("corridor_floor") if current else None,
                "rate": current.get("policy_rate") if current else None,
                "ceiling": current.get("corridor_ceiling") if current else None,
                "width_pp": (round(current["corridor_ceiling"] - current["corridor_floor"], 2)
                             if current and current.get("corridor_ceiling") is not None and current.get("corridor_floor") is not None else None),
            },
            "rate_path": [{"date": o["period_end"], "value": o["value"]} for o in rate_series],
            "corridor_path": {k: [{"date": o["period_end"], "value": o["value"]} for o in v] for k, v in corridor.items()},
            "review": self._publication_view(review),
            "previous_review": self._publication_view(prior_review),
            "forecasts": forecasts,
            "decisions_this_year": [d["announcement_date"] for d in decisions if d["announcement_date"][:4] == str(self.as_of.year)],
            "next_decision": self._next_decision(current),
        }

    def _decision_view(self, d: dict[str, Any] | None) -> dict[str, Any] | None:
        if not d:
            return None
        pub = self._pub_of(d.get("publication_id"))
        return {
            "announcement_date": d["announcement_date"],
            "effective_date": d.get("effective_date"),
            "effective_date_basis": d.get("effective_date_basis"),
            "policy_rate": d.get("policy_rate"), "corridor_floor": d.get("corridor_floor"),
            "corridor_ceiling": d.get("corridor_ceiling"),
            "rate_change_bp": d.get("rate_change_bp"), "action": d.get("action"),
            "rationale": (d.get("rationale_text") or "")[:2500],
            "rationale_language": d.get("rationale_language"),
            "publication": self._publication_view(pub),
            "source_doc_id": d.get("source_doc_id"), "rate_source_doc_id": d.get("rate_source_doc_id"),
        }

    def _stance(self, current: dict[str, Any] | None, previous: dict[str, Any] | None) -> dict[str, Any]:
        """Stance comparison with the preceding decision, stated with its evidence.

        The label describes the rate decision. An unchanged rate with a changed rationale is
        reported as exactly that, never as a tightening or an easing.
        """
        if not current:
            return {"label": "not established", "evidence": [], "rationale_changed": None}
        cur_row = pol.DecisionRow(date=dt.date.fromisoformat(current["announcement_date"]), rate=current.get("policy_rate"),
                                  floor=current.get("corridor_floor"), ceiling=current.get("corridor_ceiling"))
        prev_row = None
        if previous:
            prev_row = pol.DecisionRow(date=dt.date.fromisoformat(previous["announcement_date"]), rate=previous.get("policy_rate"),
                                       floor=previous.get("corridor_floor"), ceiling=previous.get("corridor_ceiling"))
        cur_rel = pol.DecisionRelease(rationale=current.get("rationale_text") or "")
        prev_rel = pol.DecisionRelease(rationale=(previous or {}).get("rationale_text") or "")
        out = pol.stance_comparison(cur_row, prev_row, cur_rel, prev_rel)
        out["current_action"] = current.get("action")
        out["statement"] = self._stance_sentence(out, current, previous)
        return out

    @staticmethod
    def _stance_sentence(stance: dict[str, Any], current: dict[str, Any], previous: dict[str, Any] | None) -> str:
        bp = stance.get("rate_change_bp")
        if bp is None:
            return "The change in the policy stance could not be established from the published decisions."
        if bp == 0:
            base = (f"The refinancing rate was left at {current.get('policy_rate')}% on "
                    f"{current['announcement_date']}, unchanged from {previous['announcement_date']}."
                    if previous else f"The refinancing rate stands at {current.get('policy_rate')}%.")
            if stance.get("rationale_changed"):
                base += " The stated rationale changed between the two decisions."
            return base
        direction = "cut" if bp < 0 else "raised"
        return (f"The refinancing rate was {direction} by {abs(bp):.0f} basis points to {current.get('policy_rate')}% "
                f"on {current['announcement_date']}.")

    def _next_decision(self, current: dict[str, Any] | None) -> dict[str, Any]:
        """The next decision date as announced. Nothing is inferred from the meeting cadence here."""
        if current and current.get("next_decision_date"):
            return {"date": current["next_decision_date"], "status": "announced",
                    "basis": current.get("next_decision_basis") or "announced in the decision statement"}
        return {"date": None, "status": "not announced",
                "basis": "the most recent decision statement does not name the next date"}

    def forecast_block(self, review: dict[str, Any] | None, prior_review: dict[str, Any] | None) -> dict[str, Any]:
        """CBA projections by vintage, with revisions matched on the same target period.

        Forecasts are never mixed with outcomes and are compared only like for like: same series,
        same target period, same scenario.
        """
        obs = self.observations("cba.forecast.")
        by_vintage: dict[str, list[dict[str, Any]]] = {}
        for o in obs:
            by_vintage.setdefault(o.get("forecast_vintage") or "", []).append(o)
        vintages = sorted(v for v in by_vintage if v)
        current_v = vintages[-1] if vintages else None
        prior_v = vintages[-2] if len(vintages) > 1 else None
        current = by_vintage.get(current_v or "", [])
        prior = by_vintage.get(prior_v or "", [])
        revisions = pol.compare_forecasts(
            [{"series_id": o["series_id"], "period_end": o["period_end"], "value": o["value"],
              "scenario": o.get("scenario"), "horizon_label": o.get("horizon_label"), "basis": o.get("basis"),
              "forecast_vintage": o.get("forecast_vintage")} for o in current],
            [{"series_id": o["series_id"], "period_end": o["period_end"], "value": o["value"],
              "scenario": o.get("scenario"), "horizon_label": o.get("horizon_label"), "basis": o.get("basis"),
              "forecast_vintage": o.get("forecast_vintage")} for o in prior])
        return {
            "current_vintage": current_v, "previous_vintage": prior_v,
            "current": [self.dated(o) | {"series_id": o["series_id"], "horizon": o.get("horizon_label"),
                                         "label": o.get("label_original")} for o in current],
            "revisions": revisions,
            "note": "CBA projections, not outcomes and not our own scenarios. Revisions compare the same target "
                    "period across two forecast rounds; a target published in only one round is marked as not comparable.",
            "review_edition": (review or {}).get("edition_label"),
            "previous_review_edition": (prior_review or {}).get("edition_label"),
        }

    # --------------------------------------------------------------- stability
    def stability_block(self) -> dict[str, Any]:
        report = self.latest("financial_stability_report")
        prior = self.latest("financial_stability_report", back=1)
        indicators = [o for o in self.observations("cba.fsr.") if not o["series_id"].startswith("cba.fsr.stress")]
        latest_by_series: dict[str, dict[str, Any]] = {}
        for o in sorted(indicators, key=lambda r: (r["series_id"], r["period_end"])):
            key = o["series_id"] + json.dumps(o["dims"], sort_keys=True)
            latest_by_series[key] = o
        dashboard = []
        for key, o in sorted(latest_by_series.items()):
            prev = self._previous_value(indicators, o)
            comparable = bool(prev and prev.get("consecutive"))
            entry = self.dated(o) | {
                "series_id": o["series_id"], "label": o.get("label_original"), "previous": prev,
                "change": (round(o["value"] - prev["value"], 2)
                           if comparable and prev.get("value") is not None and o["value"] is not None else None),
                "change_note": (None if comparable or not prev else
                                f"the previous reading is {prev.get('editions_apart')} editions earlier "
                                f"({prev.get('observation_date')}); no change is shown")}
            dashboard.append(entry)
        stress = [self.dated(o) | {"series_id": o["series_id"], "label": o.get("label_original"),
                                   "horizon": o.get("horizon_label")} for o in self.observations("cba.fsr.stress")]
        return {
            "report": self._publication_view(report),
            "previous_report": self._publication_view(prior),
            "dashboard": dashboard,
            "stress_tests": {
                "results": sorted(stress, key=lambda r: (r.get("scenario") or "", r["observation_date"])),
                "note": "Stress-test projections under the Central Bank's published scenarios. They are neither "
                        "forecasts nor outcomes, and they start from the exercise date shown.",
            },
            "source_reconciliation": self.source_reconciliation(self.metrics),
            "as_of_note": "Stability measures are published half-yearly and annually. They are shown at their own "
                          "reporting date together with the release date, and are never carried forward to the "
                          "banking month of this edition.",
        }

    def _previous_value(self, rows: list[dict[str, Any]], current: dict[str, Any]) -> dict[str, Any] | None:
        """The same measure in the immediately preceding edition of the same report.

        A comparison is only shown when the two readings are consecutive editions. Where an
        indicator was absent from the intervening report, the gap is stated instead of silently
        differencing readings years apart.
        """
        earlier = [r for r in rows if r["series_id"] == current["series_id"]
                   and json.dumps(r["dims"], sort_keys=True) == json.dumps(current["dims"], sort_keys=True)
                   and r["period_end"] < current["period_end"]]
        if not earlier:
            return None
        prev = sorted(earlier, key=lambda r: r["period_end"])[-1]
        editions = [p for p in self.editions("financial_stability_report")]
        keys = [p.get("reporting_period_end") for p in editions]
        try:
            gap = keys.index(current["period_end"]) - keys.index(prev["period_end"])
        except ValueError:
            gap = None
        return {"value": prev["value"], "observation_date": prev["period_end"], "editions_apart": gap,
                "consecutive": gap == 1}

    def source_reconciliation(self, metrics: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        """Where two sources publish the same concept, show both with their definitions.

        Neither value overwrites the other and neither is dropped: the statistical table is the
        primary measure for the months it covers, the report is the cross-reference, and the
        difference in definition is stated so the two are not read as a discrepancy.
        """
        metrics = metrics or {}
        out = []
        for spec in SOURCE_PRIORITY:
            rows = self.db.conn.execute(
                "SELECT series_id, period_end, value, unit, basis, population FROM observations "
                "WHERE status='current' AND series_id IN (?, ?) ORDER BY series_id, period_end",
                (spec["primary"], spec["secondary"])).fetchall()
            prim = [r for r in rows if r["series_id"] == spec["primary"]]
            computed = metrics.get(spec["primary"])
            sec = [r for r in rows if r["series_id"] == spec["secondary"]]
            if not prim and not sec:
                continue
            out.append({
                "concept": spec["concept"], "note": spec["note"],
                "primary": ({"series_id": spec["primary"], "value": prim[-1]["value"], "period": prim[-1]["period_end"],
                             "unit": prim[-1]["unit"], "definition": prim[-1]["basis"], "population": prim[-1]["population"]}
                            if prim else ({"series_id": spec["primary"], "value": (computed.get("latest") or {}).get("value"),
                                           "period": (computed.get("latest") or {}).get("period"), "unit": computed.get("unit"),
                                           "definition": computed.get("basis") or computed.get("formula"),
                                           "population": computed.get("population")} if computed else None)),
                "secondary": ({"series_id": spec["secondary"], "value": sec[-1]["value"], "period": sec[-1]["period_end"],
                               "unit": sec[-1]["unit"], "definition": sec[-1]["basis"], "population": sec[-1]["population"]}
                              if sec else None),
                "resolution": "kept separate: the definitions differ, so neither value replaces the other",
            })
        return out

    # ------------------------------------------------------------ publications
    def _publication_view(self, p: dict[str, Any] | None) -> dict[str, Any] | None:
        if not p:
            return None
        docs = self.documents(p["publication_id"])
        return {
            "publication_id": p["publication_id"], "type": PUB_LABELS.get(p["pub_type"], p["pub_type"]),
            "pub_type": p["pub_type"], "edition": p.get("edition_label"), "edition_key": p.get("edition_key"),
            "reporting_period_start": p.get("reporting_period_start"), "reporting_period_end": p.get("reporting_period_end"),
            "reporting_frequency": p.get("reporting_frequency"),
            "published_at": p.get("published_at"), "published_at_basis": p.get("published_at_basis"),
            # kept apart on purpose: the release, the translation and our download are three dates
            "translation_available_at": p.get("translation_available_at"),
            "translation_available_basis": p.get("translation_available_basis"),
            "original_language": p.get("original_language"), "first_seen_at": p.get("first_seen_at"),
            "note": p.get("note"),
            "languages": sorted({d.get("language") for d in docs if d.get("language")}),
            "documents": [{"doc_id": d["doc_id"], "language": d.get("language"), "url": d.get("document_url"),
                           "sha256": (d.get("sha256") or "")[:16], "version": d.get("version"),
                           "retrieved_at": d.get("retrieved_at"), "pages": d.get("page_count")} for d in docs],
        }

    def register(self) -> list[dict[str, Any]]:
        """Every publication visible at the cutoff, newest first, for the source register."""
        rows = [self._publication_view(p) for p in self.publications]
        return sorted([r for r in rows if r], key=lambda r: (r["pub_type"], r["edition_key"] or ""), reverse=True)

    def archive_gaps(self) -> list[dict[str, Any]]:
        """Editions the archive does not carry, so a gap is never read as "nothing happened"."""
        gaps: list[dict[str, Any]] = []
        for pub_type, expected in (("financial_stability_report", "half-yearly since 2020"),
                                   ("monetary_policy_review", "quarterly since 2020"),
                                   ("policy_directions", "annual since 2020")):
            rows = self.editions(pub_type)
            if not rows:
                gaps.append({"pub_type": PUB_LABELS[pub_type], "gap": "no edition collected", "expected": expected})
                continue
            first = rows[0].get("edition_key") or ""
            if first[:4] > "2020":
                gaps.append({"pub_type": PUB_LABELS[pub_type], "gap": f"archive starts at {rows[0].get('edition_label')}",
                             "expected": expected,
                             "note": "earlier editions are not published on the current CBA page"})
        return gaps

    def new_since(self, iso_timestamp: str) -> list[dict[str, Any]]:
        """Publications first seen after a timestamp: the release events a digest reports."""
        out = []
        for p in self.publications:
            if (p.get("first_seen_at") or "") > iso_timestamp:
                out.append(self._publication_view(p))
        return sorted(out, key=lambda r: r["published_at"] or "", reverse=True)

    def held_for_review(self) -> list[dict[str, Any]]:
        """Readings a cross-edition check could not accept, with the sentence they came from.

        They are excluded from every figure in the report and listed here so the exclusion is
        visible rather than silent.
        """
        rows = self.db.conn.execute(
            "SELECT series_id, dims, period_end, value, unit, value_raw, cell_ref, publication_id "
            "FROM observations WHERE status='current' AND validation_status='needs_review' "
            "ORDER BY series_id, period_end").fetchall()
        return [{"series_id": r["series_id"], "period_end": r["period_end"], "value": r["value"], "unit": r["unit"],
                 "publication_id": r["publication_id"], "citation": r["cell_ref"],
                 "sentence": (r["value_raw"] or "")[:200],
                 "reason": "inconsistent with the other editions of this series; excluded until checked"}
                for r in rows]

    def build(self) -> dict[str, Any]:
        return {
            "held_for_review": self.held_for_review(),
            "policy": self.policy_block(),
            "stability": self.stability_block(),
            "register": self.register(),
            "archive_gaps": self.archive_gaps(),
            "all": [{"publication_id": p["publication_id"], "pub_type": p["pub_type"], "edition_key": p.get("edition_key"),
                     "edition_label": p.get("edition_label"), "published_at": p.get("published_at"),
                     "reporting_period_end": p.get("reporting_period_end")} for p in self.publications],
            "cutoff_note": f"publications released after {self.cutoff} are excluded from this edition",
            "cutoff_limitations": ({"unknown_release_date": self.excluded_unknown_date,
                                    "note": "these publications carry no verifiable release date, so their availability "
                                            "at the cutoff could not be established and they were left out"}
                                   if self.excluded_unknown_date else None),
        }
