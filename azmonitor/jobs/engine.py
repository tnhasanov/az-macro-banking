"""The reporting engine, as the job worker drives it.

Nothing here computes a figure or renders a slide. Every call goes to the same functions the
scheduled runner and the command line use — `generate_monthly`, `generate_brief`,
`generate_weekly`, `generate_sector`, `Pipeline.refresh` — so a report produced because someone
pressed "Generate" in the browser is produced by exactly the code that produces the scheduled one.

The adapter exists so the worker can be tested without a 380 MB dataset and LibreOffice: the tests
substitute an object with the same methods. The real one is what runs everywhere else.
"""
from __future__ import annotations

import datetime as dt
import json
import os
from typing import Any

from .. import config
from ..util.log import get_logger
from . import params as PR

log = get_logger("jobs.engine")

PUB_TYPE = {k: v["publication_type"] for k, v in PR.REPORT_TYPES.items() if v.get("publication_type")}


class Engine:
    def __init__(self, *, offline: bool = False):
        self.offline = offline
        self.pipeline = None
        self.db = None

    # ------------------------------------------------------------------ lifecycle
    def open(self) -> None:
        from ..pipeline import Pipeline

        self.pipeline = Pipeline(offline=self.offline)
        self.db = self.pipeline.db

    def close(self) -> None:
        if self.db is not None:
            self.db.close()
        self.pipeline = self.db = None

    # ------------------------------------------------------------------ collecting
    def refresh(self) -> dict[str, Any]:
        # A local end-to-end run may restrict the check to named datasets (the worker refuses this
        # setting for a production job); everywhere else every configured source is checked.
        only = [d for d in (os.environ.get("AZMONITOR_REFRESH_DATASETS") or "").split(",") if d.strip()]
        return self.pipeline.refresh(dataset_ids=only or None)

    def validate(self) -> dict[str, Any]:
        from ..calc.validate import validate_all

        return validate_all(self.db, write=True)

    # ------------------------------------------------------------------ what is there
    def dataset_periods(self) -> dict[str, dict[str, Any]]:
        return {k: {"latest_period_end": v["latest_period_end"],
                    "last_changed_at": v["last_changed_at"] if "last_changed_at" in v.keys() else None,
                    "last_checked_at": v["last_checked_at"] if "last_checked_at" in v.keys() else None}
                for k, v in self.db.dataset_states().items()}

    def publications(self, report_type: str) -> list[dict[str, Any]]:
        """Held publications of the type a brief is about, most recently released last."""
        rows = [dict(p) for p in self.db.publications(PUB_TYPE[report_type])]
        rows.sort(key=lambda p: (p.get("published_at") or p.get("announcement_date") or "", p["publication_id"]))
        return rows

    def banking_month(self) -> str | None:
        """The month a monthly edition built now would be about: the common latest banking period."""
        anchors = (config.reports_config()["monthly"].get("anchors") or {}).get("banking") or []
        periods = self.dataset_periods()
        ends = [(periods.get(d) or {}).get("latest_period_end") for d in anchors]
        if not ends or not all(ends):
            return None
        return min(ends)[:7]

    def availability(self, today: dt.date) -> dict[str, Any]:
        """What the dashboard shows beside the Generate form: the period each report would cover.

        Banking tables, national accounts and prices are published on different calendars, so a
        monthly edition routinely combines banking data for one month with prices for the next and
        GDP for the last quarter. The form says so rather than implying one period covers all.
        """
        from ..scheduling.tasks import previous_calendar_week

        periods = self.dataset_periods()
        anchors = config.reports_config()["monthly"].get("anchors") or {}
        roles: dict[str, Any] = {}
        for role, datasets in anchors.items():
            ends = [(periods.get(d) or {}).get("latest_period_end") for d in datasets]
            roles[role] = {"datasets": datasets, "period_end": min(ends) if ends and all(ends) else None}
        start, end = previous_calendar_week()
        pubs: dict[str, list[dict[str, Any]]] = {}
        for rt in PUB_TYPE:
            pubs[rt] = [{"publication_id": p["publication_id"], "edition": p.get("edition_label") or p.get("edition_key"),
                         "published_at": p.get("published_at") or p.get("announcement_date")}
                        for p in self.publications(rt)[-12:]][::-1]
        return {
            "computed_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
            "monthly": {"edition_month": self.banking_month(), "anchors": roles,
                        "note": "Banking tables, national accounts and prices are published on different "
                                "calendars; an edition states the period of every input it uses."},
            "sector": {"edition_month": self.banking_month(), "sectors": PR.sectors()},
            "weekly": {"latest_complete_week": {"start": start.isoformat(), "end": end.isoformat()}},
            "publications": pubs,
            "datasets": periods,
        }

    # ------------------------------------------------------------------ readiness
    def readiness(self, report_type: str, params: dict[str, Any], today: dt.date,
                  quality: dict[str, Any] | None) -> dict[str, Any] | None:
        """The rules a scheduled report must pass before it is produced. None when none apply."""
        from ..scheduling import readiness as R

        releases = config.schedule_config().get("releases") or {}
        cfg = releases.get(report_type) or {}
        rules = cfg.get("readiness") or {}
        if report_type == "monthly":
            res = R.evaluate_monthly(self.db, rules, today, quality)
        elif report_type == "sector":
            res = R.evaluate_sector(self.db, rules, today)
        elif report_type in PUB_TYPE:
            pub = self.resolve_publication(report_type, params.get("publication_id") or "latest")
            if pub is None:
                return {"ok": False, "state": "waiting", "reasons": [{"rule": "publication", "met": False}]}
            res = R.evaluate_publication(self.db, report_type, rules, pub, today)
        else:
            return None
        gate = R.quality_gate(report_type, rules, quality)
        return (gate or res).as_dict()

    def resolve_publication(self, report_type: str, publication_id: str) -> dict[str, Any] | None:
        pubs = self.publications(report_type)
        if publication_id == "latest":
            return pubs[-1] if pubs else None
        return next((p for p in pubs if p["publication_id"] == publication_id), None)

    # ------------------------------------------------------------------ producing
    def generate(self, report_type: str, request: dict[str, Any], *, force: bool) -> dict[str, Any]:
        settings = config.settings()
        lang = settings.get("language", "en")
        if report_type == "monthly":
            from ..reports import generate_monthly

            narrative = settings.get("narrative") or {}
            return generate_monthly(None, narrative.get("provider", "none") != "api", narrative.get("file") or None,
                                    lang, force=force, db=self.db)
        if report_type == "weekly":
            from ..render.weekly import generate_weekly

            return generate_weekly(None, request["since"], lang, force=force, db=self.db,
                                   until=request["until"], produce_when_empty=True)
        if report_type == "sector":
            from ..render.sector import generate_sector

            return generate_sector(None, request["sector"], lang, force=force, db=self.db)
        if report_type in PUB_TYPE:
            from ..reports import generate_brief

            return generate_brief(report_type, as_of=None, lang=lang, publication_id=request["publication_id"],
                                  force=force, db=self.db)
        raise ValueError(report_type)

    # ------------------------------------------------------------------ the local edition record
    def edition_row(self, edition_id: str) -> dict[str, Any] | None:
        row = self.db.conn.execute("SELECT * FROM report_editions WHERE edition_id = ?", (edition_id,)).fetchone()
        return dict(row) if row else None

    def mark_edition(self, edition_id: str, status: str) -> None:
        with self.db.tx() as c:
            c.execute("UPDATE report_editions SET status = ? WHERE edition_id = ?", (status, edition_id))

    def sync_published(self, published: list[dict[str, Any]]) -> int:
        """Make the dataset agree with Postgres about which editions exist.

        A worker publishes an edition and then saves the dataset; if it dies in between, the dataset
        restored by the next worker has never heard of that edition. It would give the next edition
        the same version number, and it could not recognise an unchanged input. Publication records
        are the authority, so the missing rows are written back from them before anything runs.
        """
        have = {r["edition_id"]: dict(r) for r in self.db.conn.execute("SELECT * FROM report_editions")}
        added = 0
        for rec in published:
            local = have.get(rec["edition_id"])
            if local is not None:
                if local.get("status") != "generated":
                    self.mark_edition(rec["edition_id"], "generated")
                continue
            detail = (rec.get("manifest") or {}).get("fingerprint_detail") or {"fingerprint": rec.get("fingerprint")}
            self.db.add_edition({
                "edition_id": rec["edition_id"], "report_type": rec["report_type"],
                "edition_period": rec["edition"], "version": int(rec["version"]),
                "generated_at": rec["published_at"].isoformat() if hasattr(rec["published_at"], "isoformat")
                else str(rec["published_at"]),
                "as_of": None, "snapshot_id": None, "status": "generated", "path": "", "manifest_path": "",
                "anchors": json.dumps(rec.get("reporting_periods") or {}), "fingerprint": rec.get("fingerprint"),
                "fingerprint_detail": json.dumps(detail, default=str), "scope_key": rec["edition"]})
            added += 1
        return added
