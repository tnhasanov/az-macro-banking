"""Whether the data behind a report is in a fit state to report on.

This answers a narrower question than "has anything changed". The edition fingerprint already
decides that, and an unchanged input produces no new edition however ready it is. Readiness asks
something else: the month has turned, or a publication has been released — is what we hold about it
complete enough, fresh enough and verified enough to put in front of a board?

Three principles run through the rules:

* **A missing input is a reason to wait, not to publish a gap.** Required datasets hold an edition
  back entirely. Companion datasets hold it back only for a grace period, after which the edition is
  produced and labelled partial, naming what is absent.
* **Old is not the same as unchanged.** A source that stopped updating looks identical to one with
  nothing to report until you compare it against how often it is supposed to appear. A stale anchor
  raises an alert instead of quietly publishing last month's numbers again.
* **Every refusal says why.** A readiness result carries the rule that failed, the value it saw and
  what would satisfy it, so a run that publishes nothing is still legible the next morning.
"""
from __future__ import annotations

import datetime as dt
from typing import Any

from .. import config
from ..util.log import get_logger

log = get_logger("readiness")


class Readiness:
    """The outcome of evaluating one report's rules: ready, waiting, partial or stale."""

    def __init__(self, report: str, state: str, reasons: list[dict[str, Any]] | None = None,
                 missing: list[str] | None = None, detail: dict[str, Any] | None = None):
        self.report = report
        self.state = state                      # ready | waiting | partial | stale | blocked
        self.reasons = reasons or []
        self.missing = missing or []
        self.detail = detail or {}

    @property
    def ok(self) -> bool:
        """Whether a report may be produced. A partial edition is produced and labelled as one."""
        return self.state in ("ready", "partial")

    def as_dict(self) -> dict[str, Any]:
        return {"report": self.report, "state": self.state, "ok": self.ok, "reasons": self.reasons,
                "missing": self.missing, **self.detail}

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<Readiness {self.report} {self.state} {self.reasons}>"


def _reason(rule: str, met: bool, saw: Any, wanted: Any, note: str = "") -> dict[str, Any]:
    return {"rule": rule, "met": met, "saw": saw, "wanted": wanted, **({"note": note} if note else {})}


def _dataset_periods(db) -> dict[str, dict[str, Any]]:
    return {k: dict(v) for k, v in db.dataset_states().items()}


def _age_days(iso: str | None, today: dt.date) -> int | None:
    if not iso:
        return None
    try:
        return (today - dt.date.fromisoformat(iso[:10])).days
    except ValueError:
        return None


def last_published(db, dataset_ids: list[str]) -> tuple[str | None, str | None]:
    """When the source itself last put something out, and which dataset that was.

    Staleness is measured from this, not from the period the data describe. A monthly series is
    always reporting on a month that ended weeks ago - the July banking tables appear in late
    August - so judging by period end would call every healthy series stale in the days before its
    next release. What matters is whether the publisher has gone quiet.
    """
    best: tuple[str | None, str | None] = (None, None)
    for dsid in dataset_ids:
        row = db.conn.execute(
            "SELECT MAX(published_at) FROM documents WHERE dataset_id=? AND status IN ('parsed','stored')",
            (dsid,)).fetchone()
        stamp = row[0] if row else None
        if stamp and (best[0] is None or stamp > best[0]):
            best = (stamp, dsid)
    return best


def evaluate_monthly(db, rules: dict[str, Any], today: dt.date, quality: dict[str, Any] | None = None) -> Readiness:
    """Readiness of the monthly edition: anchor present, required tables in, companions or grace."""
    states = _dataset_periods(db)
    reasons: list[dict[str, Any]] = []
    required = rules.get("required_datasets") or []

    anchors = []
    missing_required = []
    for dsid in required:
        st = states.get(dsid) or {}
        period = st.get("latest_period_end")
        parsed = (st.get("status") or "").startswith(("parsed", "unchanged"))
        if not period or not parsed:
            missing_required.append(dsid)
        else:
            anchors.append(period)
    reasons.append(_reason("required_datasets", not missing_required,
                           f"{len(required) - len(missing_required)} of {len(required)} present",
                           "all required datasets carry a parsed period"))
    if missing_required:
        return Readiness("monthly", "waiting", reasons, missing_required,
                         {"note": "the edition waits: a required banking table has not arrived or did not parse"})

    anchor = min(anchors)
    detail: dict[str, Any] = {"anchor": anchor}

    # A publisher that has gone quiet is a stale source, not a quiet month. Measured from the last
    # release, not from the period end: the July tables appear in late August, so a healthy series
    # always describes a period several weeks old.
    max_age = rules.get("max_days_since_source_published")
    published, from_dataset = last_published(db, required)
    detail["source_last_published"] = published
    age = _age_days(published, today)
    if max_age is not None and age is not None:
        fresh = age <= int(max_age)
        reasons.append(_reason("max_days_since_source_published", fresh,
                               f"last release {published} ({age} days ago, {from_dataset})",
                               f"at most {max_age} days"))
        if not fresh:
            return Readiness("monthly", "stale", reasons, [],
                             {**detail, "note": "the banking tables have not been republished within their usual "
                                                "cadence; this is reported as a stale source rather than published "
                                                "as a new edition"})

    companions = rules.get("companion_datasets") or []
    late = [c for c in companions
            if not (states.get(c) or {}).get("latest_period_end")
            or (states.get(c) or {})["latest_period_end"] < anchor]
    if not late:
        reasons.append(_reason("companion_datasets", True, "all present", "all companion tables carry the anchor month"))
        return Readiness("monthly", "ready", reasons, [], detail)

    grace = int(rules.get("companion_grace_days", 7))
    # the clock starts when the anchor was released, not when its month ended
    waited = _age_days(published, today) if published else (_age_days(anchor, today) or 0)
    waited = waited if waited is not None else 0
    if waited < grace:
        reasons.append(_reason("companion_datasets", False, f"{len(late)} late, waited {waited} of {grace} days",
                               "companion tables present, or the grace period elapsed"))
        return Readiness("monthly", "waiting", reasons, late,
                         {**detail, "note": f"waiting up to {grace} days for companion tables before publishing a "
                                            f"partial edition"})
    reasons.append(_reason("companion_datasets", False, f"{len(late)} still late after {waited} days",
                           f"grace period of {grace} days elapsed: publish and label partial"))
    return Readiness("monthly", "partial", reasons, late,
                     {**detail, "note": "the grace period elapsed; the edition is produced and labelled partial, "
                                        "naming the tables that are absent"})


def evaluate_publication(db, report: str, rules: dict[str, Any], publication: dict[str, Any],
                         today: dt.date) -> Readiness:
    """Readiness of a brief about one publication: released recently, extracted, and verified."""
    reasons: list[dict[str, Any]] = []
    pub_id = publication.get("publication_id")
    detail: dict[str, Any] = {"publication_id": pub_id}

    # The release date, never the date we downloaded it. A backfill loads years of archive in one
    # run and none of it is news; this is the rule that keeps a backfill quiet.
    released = publication.get("published_at") or publication.get("announcement_date")
    window = rules.get("release_within_days")
    if not released:
        reasons.append(_reason("release_date_known", False, "no verified release date",
                               "a release date established from the publication itself"))
        return Readiness(report, "blocked", reasons, [],
                         {**detail, "note": "without a release date there is nothing to call this publication new"})
    age = _age_days(released, today)
    detail["released"] = released
    detail["age_days"] = age
    if window is not None and age is not None:
        recent = age <= int(window)
        reasons.append(_reason("release_within_days", recent, f"released {age} days ago", f"within {window} days"))
        if not recent:
            return Readiness(report, "waiting", reasons, [],
                             {**detail, "note": "this is archive rather than a release; it is stored and citable but "
                                                "does not trigger a brief"})

    passages = db.conn.execute("SELECT COUNT(*) FROM passages WHERE publication_id=?", (pub_id,)).fetchone()[0]
    need = int(rules.get("min_passages", 1))
    enough = passages >= need
    reasons.append(_reason("min_passages", enough, passages, f"at least {need}"))
    detail["passages"] = passages
    if not enough:
        return Readiness(report, "waiting", reasons, [],
                         {**detail, "note": "too little text was extracted to brief from; the file may be a scan "
                                            "awaiting OCR or a placeholder upload"})

    want_status = rules.get("require_extraction")
    if want_status:
        verified = db.conn.execute(
            "SELECT COUNT(*) FROM passages WHERE publication_id=? AND validation_status=?",
            (pub_id, want_status)).fetchone()[0]
        share = verified / passages if passages else 0.0
        good = share >= 0.5
        reasons.append(_reason("require_extraction", good, f"{verified} of {passages} {want_status}",
                               f"most passages {want_status}"))
        detail["verified_passages"] = verified
        if not good:
            return Readiness(report, "waiting", reasons, [],
                             {**detail, "note": f"most of this publication is not {want_status}; briefing from it "
                                                f"would quote text nothing has confirmed"})

    for field in rules.get("require_decision_fields") or []:
        row = db.conn.execute(
            f"SELECT {field} FROM policy_decisions WHERE publication_id=? AND {field} IS NOT NULL LIMIT 1",
            (pub_id,)).fetchone()
        present = row is not None
        reasons.append(_reason(f"require_decision_fields.{field}", present,
                               row[0] if row else None, "a parsed value"))
        if not present:
            return Readiness(report, "waiting", reasons, [],
                             {**detail, "note": f"the decision record has no {field}; the press release has not been "
                                                f"parsed into a usable decision yet"})

    return Readiness(report, "ready", reasons, [], detail)


def evaluate_sector(db, rules: dict[str, Any], today: dt.date) -> Readiness:
    """Readiness of a sector review: its own inputs, on top of the monthly edition it follows."""
    states = _dataset_periods(db)
    reasons: list[dict[str, Any]] = []
    missing = [d for d in (rules.get("required_datasets") or [])
               if not (states.get(d) or {}).get("latest_period_end")]
    reasons.append(_reason("required_datasets", not missing, f"{len(missing)} missing", "all present"))
    if missing:
        return Readiness("sector", "waiting", reasons, missing,
                         {"note": "the sector review waits for the tables it is built from"})

    max_age = rules.get("max_days_since_source_published")
    if max_age is not None:
        ages = {}
        for d in (rules.get("required_datasets") or []):
            stamp, _ = last_published(db, [d])
            ages[d] = _age_days(stamp, today)
        oldest = max((a for a in ages.values() if a is not None), default=None)
        if oldest is not None and oldest > int(max_age):
            reasons.append(_reason("max_days_since_source_published", False,
                                   f"quietest input last published {oldest} days ago", f"at most {max_age} days"))
            return Readiness("sector", "stale", reasons, [], {"days_since_published": ages})
        reasons.append(_reason("max_days_since_source_published", True,
                               f"quietest input last published {oldest} days ago", f"at most {max_age} days"))
    return Readiness("sector", "ready", reasons, [])


def quality_gate(report: str, rules: dict[str, Any], quality: dict[str, Any] | None) -> Readiness | None:
    """A critical data-quality failure blocks the reports that say they need a clean bill.

    Returns None when the gate passes or does not apply, so a caller can treat it as an override of
    an otherwise-ready result.
    """
    if not rules.get("require_quality_pass"):
        return None
    critical = int(((quality or {}).get("summary") or quality or {}).get("critical", 0) or 0)
    if critical == 0:
        return None
    return Readiness(report, "blocked",
                     [_reason("require_quality_pass", False, f"{critical} critical failure(s)", "no critical failures")],
                     [], {"note": "a data-quality check failed inside the window this report displays; the previous "
                                  "edition stays current until it is resolved or an explicit exception is recorded"})


def stale_sources(db, rules: dict[str, Any], today: dt.date) -> list[dict[str, Any]]:
    """Sources that have not changed for longer than their own cadence allows.

    A source that has gone quiet and a source that has nothing to say look identical from the
    outside; the only way to tell them apart is to know how often it is supposed to appear.
    """
    out: list[dict[str, Any]] = []
    default = int((rules.get("stale_after_days") or {}).get("default", 45))
    per_dataset = rules.get("stale_after_days") or {}
    for dsid, st in _dataset_periods(db).items():
        limit = int(per_dataset.get(dsid, default))
        changed = st.get("last_changed_at") or st.get("last_checked_at")
        age = _age_days(changed, today)
        if age is not None and age > limit:
            out.append({"dataset_id": dsid, "last_changed": (changed or "")[:10], "age_days": age,
                        "stale_after_days": limit,
                        "note": f"no change in {age} days; this source is expected to move within {limit}"})
    return sorted(out, key=lambda r: -r["age_days"])


def evaluate_all(db, today: dt.date, quality: dict[str, Any] | None = None) -> dict[str, Readiness]:
    """Every configured report's readiness, for a dry run or a status view."""
    sched = config.schedule_config()
    out: dict[str, Readiness] = {}
    for name, cfg in (sched.get("releases") or {}).items():
        rules = cfg.get("readiness") or {}
        trigger = cfg.get("trigger")
        if trigger == "banking_anchor_advanced":
            res = evaluate_monthly(db, rules, today, quality)
        elif trigger == "monthly_edition_published":
            res = evaluate_sector(db, rules, today)
        elif trigger == "publication_released":
            # the most recently *released* edition, not the last by edition key: a half-yearly
            # report published in October sorts after an annual one published the following April
            pubs = [dict(p) for p in db.publications(cfg.get("publication_type"))]
            latest = max(pubs, key=lambda p: (p.get("published_at") or p.get("announcement_date") or ""),
                         default=None) if pubs else None
            res = (evaluate_publication(db, name, rules, latest, today) if latest
                   else Readiness(name, "waiting", [_reason("publication", False, "none stored", "a publication")]))
        else:
            res = Readiness(name, "blocked", [_reason("trigger", False, trigger, "a known trigger")])
        blocked = quality_gate(name, rules, quality)
        out[name] = blocked or res
    return out
