"""What a source check found, what it means, and which reports it affects.

A refresh reports what happened to each document it downloaded. This module turns that into
*classified changes*, because what matters is not that a file changed but what kind of event it
was:

  new_publication       a publication appeared that was released recently
  new_observations      a dataset gained a period beyond the latest it had
  substantive_revision  values already published changed by more than the materiality threshold
  translation           another language edition of a publication already held
  historical_backfill   something old, seen for the first time — an archive being filled in
  bytes_only            the file changed and the data in it did not

Only the first three produce reports or notifications. The distinction is made from dates the
engine keeps apart on purpose — when the source released a publication, when a translation
appeared, when this system first saw a document, when this copy was fetched — so a backfill of
five years of monetary policy reviews reads as five years of history, not as five new releases.

The mapping from change to report is explicit, in config/triggers.yaml, and a plan coalesces a
batch: however many tables of the monthly release arrive in one check, the monthly monitor is
planned once.
"""
from __future__ import annotations

import datetime as dt
import hashlib
from dataclasses import asdict, dataclass, field
from typing import Any, Iterable

from . import config
from .util.log import get_logger

log = get_logger("changes")

PRODUCTIVE = ("new_publication", "new_observations", "substantive_revision")
CAUSE = {"new_publication": "new_publication", "new_observations": "new_data", "substantive_revision": "revision"}
# When several changes land on one report, the plan takes the most newsworthy as its cause.
CAUSE_ORDER = ("new_publication", "new_data", "revision")


def triggers() -> dict[str, Any]:
    return config.load_yaml(config.CONFIG_DIR / "triggers.yaml")


@dataclass
class Change:
    change_id: str
    classification: str
    dataset_id: str | None
    source_id: str | None
    publication_id: str | None
    document_id: str | None
    pub_type: str | None
    published_at: str | None
    translation_available_at: str | None
    first_seen_at: str | None
    retrieved_at: str | None
    language: str | None
    periods: list[str]
    affects: list[dict[str, Any]]
    notifiable: bool
    reason: str
    detail: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _day(value: Any) -> dt.date | None:
    if not value:
        return None
    try:
        return dt.date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _window(cfg: dict[str, Any], pub_type: str | None) -> int:
    windows = cfg.get("release_within_days") or {}
    return int(windows.get(pub_type or "", windows.get("default", 45)))


def classify(rec: dict[str, Any], *, today: dt.date, cfg: dict[str, Any] | None = None) -> tuple[str, str]:
    """The classification of one changed document, and the reason in a sentence."""
    cfg = cfg or triggers()
    if rec.get("is_translation") and not rec.get("n_revisions"):
        return "translation", (f"a {rec.get('language')} edition of a publication originally released in "
                               f"{rec.get('original_language')}; a translation is not a release")
    if rec.get("new_publication"):
        released = _day(rec.get("published_at"))
        window = _window(cfg, rec.get("pub_type"))
        if released is None:
            return "historical_backfill", ("a publication seen for the first time with no stated release date; "
                                           "the download date is not a release date, so it is not treated as news")
        age = (today - released).days
        if age <= window:
            return "new_publication", f"released {released.isoformat()}, {age} day(s) ago (window {window} days)"
        return "historical_backfill", (f"first seen now but released {released.isoformat()}, {age} days ago — "
                                       f"older than the {window}-day window for news")
    latest_before = rec.get("latest_before")
    new_periods = rec.get("new_periods") or []
    if new_periods and latest_before is None:
        return "historical_backfill", "the first observations ever stored for this dataset: an initial load, not a release"
    beyond = [p for p in new_periods if latest_before and p > latest_before]
    if beyond:
        return "new_observations", f"new period(s) {', '.join(beyond[-3:])} beyond the latest held ({latest_before})"
    if rec.get("n_revisions"):
        return "substantive_revision", (f"{rec['n_revisions']} published value(s) changed beyond the materiality "
                                        f"threshold, for {', '.join((rec.get('revised_periods') or [])[-3:])}")
    if new_periods:
        return "historical_backfill", (f"observations for earlier period(s) {', '.join(new_periods[:3])} that were "
                                       f"missing — history filled in, not a new release")
    return "bytes_only", "the document changed but none of the data read from it did"


def affects(rec: dict[str, Any], classification: str, *, cfg: dict[str, Any] | None = None,
            sectors: Iterable[str] = ()) -> list[dict[str, Any]]:
    """The reports a productive change affects, from config/triggers.yaml."""
    if classification not in PRODUCTIVE:
        return []
    cfg = cfg or triggers()
    rules = []
    if rec.get("pub_type"):
        rules = (cfg.get("publication_types") or {}).get(rec["pub_type"]) or []
    if not rules and rec.get("dataset_id"):
        rules = (cfg.get("datasets") or {}).get(rec["dataset_id"]) or []
    out: list[dict[str, Any]] = []
    for rule in rules:
        report = rule["report"]
        announce = bool(rule.get("announce", True))
        if report == "sector":
            for sector in sectors:
                out.append({"report": "sector", "sector": sector, "announce": announce})
        elif report in ("mpr_brief", "fsr_brief", "decision_update"):
            out.append({"report": report, "publication_id": rec.get("publication_id"), "announce": announce})
        else:
            out.append({"report": report, "announce": announce})
    return out


def scheduled_sectors() -> list[str]:
    return list(((config.schedule_config().get("releases") or {}).get("sector") or {}).get("sectors") or [])


def classify_refresh(refresh: dict[str, Any], *, batch_id: str, today: dt.date | None = None) -> list[Change]:
    """Every document-level change in a refresh summary, classified."""
    today = today or dt.date.today()
    cfg = triggers()
    sectors = scheduled_sectors()
    out: list[Change] = []
    for dataset_id, stats in (refresh.get("datasets") or {}).items():
        for rec in stats.get("changes") or []:
            cls, reason = classify(rec, today=today, cfg=cfg)
            aff = affects(rec, cls, cfg=cfg, sectors=sectors)
            periods = sorted(set((rec.get("new_periods") or []) + (rec.get("revised_periods") or [])))
            ident = hashlib.sha256(f"{batch_id}|{rec.get('document_id')}|{dataset_id}".encode()).hexdigest()[:20]
            out.append(Change(
                change_id=f"chg_{ident}", classification=cls, dataset_id=dataset_id,
                source_id=rec.get("source_id"), publication_id=rec.get("publication_id"),
                document_id=rec.get("document_id"), pub_type=rec.get("pub_type"),
                published_at=rec.get("published_at"), translation_available_at=rec.get("translation_available_at"),
                first_seen_at=rec.get("first_seen_at"), retrieved_at=rec.get("retrieved_at"),
                language=rec.get("language"), periods=periods, affects=aff,
                notifiable=cls in PRODUCTIVE and any(a.get("announce") for a in aff), reason=reason,
                detail={"n_new": rec.get("n_new"), "n_revisions": rec.get("n_revisions"),
                        "latest_before": rec.get("latest_before"), "document_url": rec.get("document_url"),
                        "decisions": rec.get("decisions") or []}))
    return out


@dataclass
class PlannedReport:
    report_type: str
    params: dict[str, Any]
    cause: str
    announce: bool
    change_ids: list[str]
    reasons: list[str]

    @property
    def scope(self) -> str:
        return self.params.get("sector") or self.params.get("publication_id") or "latest"


def plan(changes: Iterable[Change]) -> list[PlannedReport]:
    """One report per affected (report, scope), however many changes point at it.

    The monthly release arrives as a dozen tables; a check that sees all of them plans the monthly
    once. Its cause is the most newsworthy of the changes behind it, and it is announced if any of
    them is announceable — a policy decision that refreshes the monthly's policy slide does not, on
    its own, make the monthly worth an email.
    """
    planned: dict[tuple[str, str], PlannedReport] = {}
    for ch in changes:
        if ch.classification not in PRODUCTIVE:
            continue
        cause = CAUSE[ch.classification]
        for a in ch.affects:
            params: dict[str, Any] = {"period": "latest", "refresh": "latest_data"}
            if a.get("sector"):
                params["sector"] = a["sector"]
            if a.get("publication_id"):
                params["publication_id"] = a["publication_id"]
            key = (a["report"], params.get("sector") or params.get("publication_id") or "latest")
            p = planned.get(key)
            if p is None:
                p = planned[key] = PlannedReport(a["report"], params, cause, bool(a.get("announce")), [], [])
            elif CAUSE_ORDER.index(cause) < CAUSE_ORDER.index(p.cause):
                p.cause = cause
            p.announce = p.announce or bool(a.get("announce"))
            if ch.change_id not in p.change_ids:
                p.change_ids.append(ch.change_id)
                p.reasons.append(f"{ch.dataset_id}: {ch.reason}")
    # a report refreshed only because another report announces the same event is not news itself
    for p in planned.values():
        if not p.announce:
            p.cause = "coverage_refresh"
    order = {"monthly": 0, "sector": 1, "decision_update": 2, "mpr_brief": 3, "fsr_brief": 4}
    return sorted(planned.values(), key=lambda p: (order.get(p.report_type, 9), p.scope))


def record(conn, changes: Iterable[Change], *, batch_id: str, environment: str) -> int:
    """Store the classified changes. Idempotent per batch and document."""
    from psycopg.types.json import Jsonb

    n = 0
    with conn.cursor() as cur:
        for ch in changes:
            cur.execute(
                """INSERT INTO source_changes(change_id, batch_id, environment, classification, source_id,
                       dataset_id, publication_id, document_id, published_at, translation_available_at,
                       first_seen_at, retrieved_at, language, periods, affects, notifiable, detail,
                       handled_at, handling)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
                           CASE WHEN %s THEN NULL ELSE now() END,
                           CASE WHEN %s THEN NULL ELSE 'not_productive' END)
                   ON CONFLICT (change_id) DO NOTHING""",
                (ch.change_id, batch_id, environment, ch.classification, ch.source_id, ch.dataset_id,
                 ch.publication_id, ch.document_id, _day(ch.published_at), _day(ch.translation_available_at),
                 ch.first_seen_at, ch.retrieved_at, ch.language, Jsonb(ch.periods), Jsonb(ch.affects),
                 ch.notifiable, Jsonb({**ch.detail, "reason": ch.reason}),
                 ch.classification in PRODUCTIVE, ch.classification in PRODUCTIVE))
            n += cur.rowcount
    if not conn.autocommit:
        conn.commit()
    return n


def summarise(changes: Iterable[Change]) -> dict[str, int]:
    out: dict[str, int] = {}
    for ch in changes:
        out[ch.classification] = out.get(ch.classification, 0) + 1
    return out


def pending(conn, *, environment: str, max_age_days: int = 35) -> list[Change]:
    """Every productive change no report has answered yet, oldest first.

    Changes older than `max_age_days` are closed as expired rather than planned: a monthly that has
    waited five weeks for a companion table is superseded by the next month's release, and planning
    from a stale change would announce old data as news.
    """
    with conn.cursor() as cur:
        cur.execute(
            """UPDATE source_changes SET handled_at = now(), handling = 'expired'
                WHERE environment = %s AND handled_at IS NULL
                  AND detected_at < now() - make_interval(days => %s)""",
            (environment, max_age_days))
        cur.execute(
            """SELECT change_id, classification, dataset_id, source_id, publication_id, document_id,
                      published_at, translation_available_at, first_seen_at, retrieved_at, language,
                      periods, affects, notifiable, detail
                 FROM source_changes
                WHERE environment = %s AND handled_at IS NULL
                ORDER BY detected_at, change_id""",
            (environment,))
        rows = cur.fetchall()
    if not conn.autocommit:
        conn.commit()
    out: list[Change] = []
    for r in rows:
        detail = dict(r[14] or {})
        out.append(Change(
            change_id=r[0], classification=r[1], dataset_id=r[2], source_id=r[3], publication_id=r[4],
            document_id=r[5], pub_type=None,
            published_at=r[6].isoformat() if r[6] else None,
            translation_available_at=r[7].isoformat() if r[7] else None,
            first_seen_at=r[8].isoformat() if r[8] else None,
            retrieved_at=r[9].isoformat() if r[9] else None,
            language=r[10], periods=list(r[11] or []), affects=list(r[12] or []), notifiable=bool(r[13]),
            reason=str(detail.pop("reason", "")), detail=detail))
    return out


def mark_handled(conn, change_ids: Iterable[str], *, job_id: str | None, handling: str) -> int:
    """Close changes a report has answered: published, or found to change nothing."""
    ids = list(change_ids)
    if not ids:
        return 0
    with conn.cursor() as cur:
        cur.execute(
            """UPDATE source_changes SET handled_at = now(), handling = %s,
                      handled_by_job = coalesce(handled_by_job, %s)
                WHERE change_id = ANY(%s) AND handled_at IS NULL""",
            (handling, job_id, ids))
        n = cur.rowcount
    if not conn.autocommit:
        conn.commit()
    return n
