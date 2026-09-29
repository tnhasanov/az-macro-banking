"""Making an edition published, in one transaction.

"Published" means visible to signed-in users of the application. It happens exactly once per
edition, in one Postgres transaction that does four things together:

  1. records the publication (with its verified artefact manifest),
  2. adds the edition to the dashboard's archive,
  3. queues the notifications it warrants, and
  4. marks the job that produced it as succeeded.

Together, so that a crash can never leave a published edition whose email was lost, an email
queued for an edition nobody can see, or a job reporting success for an edition that does not
exist. The transaction first re-reads the job row under a lock and checks the fence: a worker that
lost its lease cannot publish, however far it got.

Every artefact is uploaded and verified *before* this runs. The transaction describes files that
are already in storage; it never describes files that might arrive.
"""
from __future__ import annotations

import json
import os
from typing import Any

from psycopg.types.json import Jsonb

from . import new_id
from .jobs import Claim, LeaseLost, _guarded, _rows

# Why an edition exists, and whether that is news to anyone. A manual request or a forced
# regeneration is the requester's business; new official data is everyone's.
ANNOUNCED_CAUSES = ("new_data", "revision", "new_publication", "weekly")
# coverage_refresh: the report changed because of an event another report in the same batch
# announces (a policy decision refreshes the monthly's policy slide; the decision update is the news).
CAUSES = ANNOUNCED_CAUSES + ("manual_request", "admin_force", "coverage_refresh")


class PublicationConflict(RuntimeError):
    """The same report, edition and version is already published by something else."""


def _settings(cur, environment: str) -> dict[str, Any]:
    cur.execute("SELECT * FROM notification_settings WHERE environment = %s", (environment,))
    rows = _rows(cur)
    if rows:
        return rows[0]
    # No row means nobody has turned anything on: announcements are recorded but not sent.
    return {"auto_email_enabled": False, "paused": False, "notify_revisions": True,
            "attach_pdf": True, "revision_settle_minutes": 120}


def announcement_recipients(cur, report_type: str, sector: str | None) -> list[dict[str, Any]]:
    cur.execute(
        """SELECT DISTINCT r.* FROM recipients r
             JOIN subscriptions s ON s.recipient_id = r.recipient_id
            WHERE s.report_type = %s AND (s.sector = '*' OR s.sector = coalesce(%s, '*'))
              AND r.active AND r.unsubscribed_at IS NULL
            ORDER BY r.recipient_id""",
        (report_type, sector))
    return _rows(cur)


def _decide(settings: dict[str, Any], environment: str, recipient: dict[str, Any],
            purpose: str) -> tuple[str, str | None]:
    """Queued, or suppressed with the reason. Pause is not a suppression: paused rows stay queued
    and go out when notifications resume."""
    if environment != "production" and not (environment == "test" and os.environ.get("AZMONITOR_TEST_NOTIFICATIONS") == "1"):
        # "test" is the local end-to-end environment, which sends only to a capture directory
        # (web/lib/email/provider.ts refuses a real provider anywhere but production).
        return "suppressed", (f"produced in a {environment} deployment; only production emails "
                              "subscribers")
    if not settings.get("auto_email_enabled"):
        return "suppressed", "automatic email is turned off"
    if purpose == "revised_edition" and not settings.get("notify_revisions", True):
        return "suppressed", "notices of revised editions are turned off"
    if recipient.get("suppressed_at"):
        return "suppressed", f"address suppressed: {recipient.get('suppressed_reason') or 'permanent failure'}"
    return "queued", None


def commit(conn, claim: Claim, *, record: dict[str, Any], catalog_entry: dict[str, Any],
           cause: str, change_ids: list[str] | None = None,
           edition_fingerprint: dict[str, Any] | None = None) -> dict[str, Any]:
    """Publish one edition and complete its job. Raises LeaseLost or PublicationConflict."""
    if cause not in CAUSES:
        raise ValueError(f"unknown publication cause {cause!r}")
    report_type, edition, version = record["report_type"], record["edition"], int(record["version"])
    edition_id = record["edition_id"]
    env = claim.environment
    summary: dict[str, Any] = {"edition_id": edition_id, "cause": cause, "notifications": []}

    with conn.transaction():
        with conn.cursor() as cur:
            live = _guarded(cur, claim, f"publishing {edition_id}")

            cur.execute("SELECT edition_id, job_id FROM publication_records WHERE edition_id = %s "
                        "OR (report_type = %s AND edition = %s AND version = %s)",
                        (edition_id, report_type, edition, version))
            clash = cur.fetchone()
            if clash:
                raise PublicationConflict(f"{report_type} {edition} v{version} is already published "
                                          f"(by job {clash[1]})")

            cur.execute("""SELECT edition_id FROM publication_records
                            WHERE report_type = %s AND edition = %s AND version < %s
                            ORDER BY version DESC LIMIT 1""", (report_type, edition, version))
            prev = cur.fetchone()
            supersedes = prev[0] if prev else None

            cur.execute(
                """INSERT INTO publication_records(edition_id, report_type, edition, version, sector,
                       environment, job_id, cause, change_batch_id, fingerprint, manifest, validation,
                       reporting_periods, information_cutoff, findings, limitations, supersedes)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (edition_id, report_type, edition, version, record.get("sector"), env, claim.job_id,
                 cause, record.get("change_batch_id"), record.get("fingerprint"),
                 Jsonb(record["manifest"]), Jsonb(record["validation"]),
                 Jsonb(record.get("reporting_periods") or {}), record.get("information_cutoff"),
                 Jsonb(record.get("findings") or []), Jsonb(record.get("limitations") or []),
                 supersedes))

            e = catalog_entry
            cur.execute(
                "INSERT INTO editions (report_type, edition, version, generated_at, as_of, status_label, "
                "partial, n_slides, fingerprint, trigger, narrative_mode, numbers_checked, quality, "
                "reporting_periods, summary, files, blob_prefix, is_latest, files_missing, catalogued_at) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,TRUE,'[]'::jsonb,now()) "
                "ON CONFLICT (report_type, edition, version) DO UPDATE SET files = EXCLUDED.files, "
                "  blob_prefix = EXCLUDED.blob_prefix, is_latest = TRUE, summary = EXCLUDED.summary",
                (report_type, edition, version, e.get("generated_at"), e.get("as_of"), e.get("status_label"),
                 bool(e.get("partial")), e.get("n_slides"), e.get("fingerprint"), e.get("trigger"),
                 e.get("narrative_mode"), e.get("numbers_checked"), json.dumps(e.get("quality") or {}),
                 json.dumps(e.get("reporting_periods") or {}), json.dumps(e.get("summary") or []),
                 json.dumps(e.get("files") or []), e.get("blob_prefix")))
            cur.execute("UPDATE editions SET is_latest = FALSE WHERE report_type = %s AND edition = %s "
                        "AND version < %s AND is_latest", (report_type, edition, version))

            # ---- notification intents, in the same transaction as the publication
            settings = _settings(cur, env)
            purpose = "revised_edition" if supersedes else "new_edition"
            announced_to: set[str] = set()
            if cause in ANNOUNCED_CAUSES:
                if supersedes:
                    # A revision that lands before the previous version's announcement went out
                    # replaces it: the reader gets one message, about the version that exists now.
                    cur.execute(
                        """UPDATE email_outbox SET status = 'cancelled', updated_at = now(),
                                  status_reason = %s
                            WHERE edition_id IN (SELECT edition_id FROM publication_records
                                                  WHERE report_type = %s AND edition = %s AND version < %s)
                              AND purpose IN ('new_edition','revised_edition') AND status = 'queued'
                           RETURNING purpose""",
                        (f"superseded by version {version} before it was sent", report_type, edition, version))
                    if any(r[0] == "new_edition" for r in cur.fetchall()):
                        purpose = "new_edition"
                settle = int(settings.get("revision_settle_minutes") or 0) if purpose == "revised_edition" else 0
                for r in announcement_recipients(cur, report_type, record.get("sector")):
                    status, reason = _decide(settings, env, r, purpose)
                    delivery_id = new_id("dlv")
                    cur.execute(
                        """INSERT INTO email_outbox(delivery_id, environment, edition_id, job_id, recipient_id,
                               to_address, purpose, status, status_reason, idempotency_key, not_before)
                           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s, now() + make_interval(mins => %s))
                           ON CONFLICT DO NOTHING RETURNING delivery_id""",
                        (delivery_id, env, edition_id, claim.job_id, r["recipient_id"], r["email"], purpose,
                         status, reason, delivery_id, settle))
                    if cur.fetchone():
                        summary["notifications"].append({"recipient_id": r["recipient_id"], "purpose": purpose,
                                                         "status": status, "reason": reason})
                        if status == "queued":
                            announced_to.add(r["email"].lower())

            requester = live.get("requester_email")
            if live.get("notify_requester") and requester and requester.lower() not in announced_to:
                delivery_id = new_id("dlv")
                cur.execute(
                    """INSERT INTO email_outbox(delivery_id, environment, edition_id, job_id, to_address,
                           purpose, status, idempotency_key)
                       VALUES (%s,%s,%s,%s,%s,'manual_request','queued',%s)""",
                    (delivery_id, env, edition_id, claim.job_id, requester, delivery_id))
                summary["notifications"].append({"recipient": "requester", "purpose": "manual_request",
                                                 "status": "queued"})

            if change_ids:
                # Attribution only. A change is closed by the check that planned it, once every report
                # it affects has answered: the monthly publishing does not settle a change the sector
                # review is still waiting on.
                cur.execute("UPDATE source_changes SET handled_by_job = coalesce(handled_by_job, %s) "
                            "WHERE change_id = ANY(%s)", (claim.job_id, change_ids))

            if record.get("fingerprint"):
                cur.execute(
                    """INSERT INTO current_fingerprints(environment, report_type, scope_key, fingerprint, edition_id)
                       VALUES (%s,%s,%s,%s,%s)
                       ON CONFLICT (environment, report_type, scope_key) DO UPDATE
                         SET fingerprint = excluded.fingerprint, edition_id = excluded.edition_id, computed_at = now()""",
                    (env, report_type, record.get("scope_key") or edition, record["fingerprint"], edition_id))

            result = {"edition_id": edition_id, "cause": cause, "supersedes": supersedes,
                      "notifications": summary["notifications"],
                      "reporting_periods": record.get("reporting_periods") or {}}
            if edition_fingerprint:
                result["trigger"] = (edition_fingerprint.get("trigger") if isinstance(edition_fingerprint, dict)
                                     else None)
            cur.execute(
                """UPDATE report_jobs SET status = 'succeeded', stage = 'complete', stage_detail = NULL,
                          finished_at = now(), lease_expires_at = NULL, edition_id = %s,
                          result = result || %s
                    WHERE job_id = %s""",
                (edition_id, Jsonb(result), claim.job_id))
            cur.execute(
                "INSERT INTO job_events(job_id, attempt, stage, status, message, detail) "
                "VALUES (%s,%s,'complete','succeeded',%s,%s)",
                (claim.job_id, claim.attempt, f"published {edition_id}", Jsonb(result)))
    summary["supersedes"] = supersedes
    return summary


def queue_requester_email(cur, claim: Claim, edition_id: str, live: dict[str, Any]) -> bool:
    """The requester asked to be emailed and the answer is an edition that already existed.

    Called inside the transaction that finishes the job (see jobs.finish), with the row as it is
    now, so a request to be emailed made while the job ran is honoured and the email and the job's
    outcome are recorded together.
    """
    if not (live.get("notify_requester") and live.get("requester_email")):
        return False
    delivery_id = new_id("dlv")
    cur.execute(
        """INSERT INTO email_outbox(delivery_id, environment, edition_id, job_id, to_address, purpose,
               status, idempotency_key)
           SELECT %s,%s,%s,%s,%s,'manual_request','queued',%s
            WHERE EXISTS (SELECT 1 FROM publication_records WHERE edition_id = %s)""",
        (delivery_id, claim.environment, edition_id, claim.job_id, live["requester_email"], delivery_id,
         edition_id))
    return cur.rowcount == 1


def published(conn, report_type: str, edition: str | None = None) -> list[dict[str, Any]]:
    with conn.cursor() as cur:
        if edition is None:
            cur.execute("SELECT * FROM publication_records WHERE report_type = %s ORDER BY published_at",
                        (report_type,))
        else:
            cur.execute("SELECT * FROM publication_records WHERE report_type = %s AND edition = %s "
                        "ORDER BY version", (report_type, edition))
        return _rows(cur)


__all__ = ["commit", "queue_requester_email", "published", "PublicationConflict", "LeaseLost",
           "ANNOUNCED_CAUSES", "CAUSES", "announcement_recipients"]
