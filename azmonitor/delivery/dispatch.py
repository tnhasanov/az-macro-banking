"""Deciding what to send, sending it once, and recording what happened.

The order of operations is the safety property here:

    resolve recipients → compose → write the ledger row → call the provider → record the outcome

Writing the row before the call is what makes a crash survivable. Composing before the row is what
makes the row meaningful: it carries the hash of what was composed, so a corrected edition is a
different delivery and an unchanged rerun is the same one.

A run never sends the same (edition, channel, recipient) twice, never retries an ambiguous result,
and never treats "delivery is disabled" as a failure — that is a `skipped` row with the reason, so a
dry run leaves the same audit trail a real one would.
"""
from __future__ import annotations

import datetime as dt
import os
from pathlib import Path
from typing import Any

from .. import config
from ..util.log import get_logger
from . import compose as composer
from . import providers as P
from .records import DeliveryLedger

log = get_logger("delivery.dispatch")


def ledger_path() -> Path:
    """Beside the dataset, not inside it: rebuilding the data must not erase the send history."""
    return config.paths().data_dir / "deliveries.sqlite"


class Recipient:
    def __init__(self, spec: dict[str, Any], channel: str):
        self.id = spec.get("id") or "unknown"
        self.role = spec.get("role")
        self.spec = spec
        self.channel = channel
        self.address = os.environ.get(spec.get(f"{channel}_env") or "") or None

    @property
    def configured(self) -> bool:
        return bool(self.address)

    def skip_reason(self) -> str | None:
        if not self.spec.get("channels") or self.channel not in self.spec["channels"]:
            return f"recipient {self.id} does not take {self.channel}"
        if not self.address:
            return (f"no address for {self.id}: set {self.spec.get(f'{self.channel}_env')} in the environment")
        if self.channel == "whatsapp" and not self.spec.get("whatsapp_opted_in"):
            # An opt-in is a fact about a person, not a setting. Without one recorded here, nothing
            # is sent however complete the credentials are.
            return f"{self.id} has no recorded WhatsApp opt-in"
        return None


def recipients_for(report_type: str, channel: str) -> tuple[list[Recipient], list[dict[str, str]]]:
    """The people a report goes to over one channel, and why anyone was left out."""
    cfg = config.delivery_config()
    route = (cfg.get("routing") or {}).get(report_type) or {}
    list_name = route.get("list")
    dl = (cfg.get("distribution_lists") or {}).get(list_name) or {}
    out, skipped = [], []
    for spec in dl.get("recipients") or []:
        r = Recipient(spec, channel)
        reason = r.skip_reason()
        if reason:
            skipped.append({"recipient_id": r.id, "reason": reason})
        else:
            out.append(r)
    return out, skipped


def _retry_at(attempts: int, cfg: dict[str, Any]) -> str | None:
    backoff = cfg.get("backoff_seconds") or [30, 300]
    if attempts > len(backoff):
        return None                                  # attempts exhausted; it stays failed
    delay = int(backoff[min(attempts - 1, len(backoff) - 1)])
    return (dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=delay)).isoformat(timespec="seconds")


def deliver_edition(report_type: str, edition_dir: Path, *, dry_run: bool = False,
                    ledger: DeliveryLedger | None = None, sector: str | None = None) -> dict[str, Any]:
    """Compose and deliver one produced edition to everyone routed to it."""
    cfg = config.delivery_config()
    route = (cfg.get("routing") or {}).get(report_type) or {}
    if not route:
        return {"report_type": report_type, "status": "no_route",
                "note": f"no routing configured for {report_type}; nothing was composed"}

    own = ledger is None
    ledger = ledger or DeliveryLedger(ledger_path())
    summary: dict[str, Any] = {"report_type": report_type, "edition_dir": str(edition_dir),
                               "deliveries": [], "skipped": [], "enabled": bool(cfg.get("enabled")),
                               "dry_run": dry_run}
    try:
        # A row left mid-flight by a dead process is ambiguous, and is surfaced before anything new
        # is attempted, so a person sees it on the next run rather than a week later.
        stuck = ledger.recover_stuck(int((cfg.get("retry") or {}).get("stale_after_minutes", 120)))
        if stuck:
            summary["recovered_stuck"] = stuck

        for channel, ch_cfg in (cfg.get("channels") or {}).items():
            if not ch_cfg.get("enabled"):
                summary["skipped"].append({"channel": channel, "reason": f"{channel} channel is disabled"})
                continue
            people, skipped = recipients_for(report_type, channel)
            summary["skipped"] += [{"channel": channel, **s} for s in skipped]
            if not people:
                continue

            if channel == "email":
                message = composer.compose(report_type, edition_dir,
                                           subject_template=route.get("subject", "{report_type} {edition}"),
                                           message_cfg=ch_cfg.get("message") or {}, sector=sector)
            else:
                message = None

            for person in people:
                summary["deliveries"].append(
                    _deliver_one(ledger, cfg, channel, ch_cfg, person, report_type, edition_dir,
                                 message, route, dry_run, sector))
    finally:
        if own:
            ledger.close()
    summary["status"] = "ok"
    return summary


def _deliver_one(ledger: DeliveryLedger, cfg: dict[str, Any], channel: str, ch_cfg: dict[str, Any],
                 person: Recipient, report_type: str, edition_dir: Path, message, route: dict[str, Any],
                 dry_run: bool, sector: str | None) -> dict[str, Any]:
    if channel == "email":
        provider = P.email_provider(ch_cfg)
        subject, content_hash = message.subject, message.content_hash
        artifacts = message.artifacts()
        edition, version = message.edition, message.version
    else:
        provider = P.whatsapp_provider(ch_cfg)
        edition = edition_dir.parent.name
        version = None
        subject = f"{report_type} {edition}"
        content_hash = f"template:{report_type}"
        artifacts = {"template": "report_ready"}

    did, state = ledger.enqueue(report_type=report_type, edition=edition, version=version, channel=channel,
                                recipient_id=person.id, recipient_address=person.address,
                                provider=provider.name, subject=subject, content_hash=content_hash,
                                artifacts=artifacts)
    base = {"delivery_id": did, "channel": channel, "recipient_id": person.id, "edition": edition,
            "version": version, "subject": subject}

    if state == "sent":
        # the case a rerun of an unchanged edition hits: nothing composed twice, nothing sent twice
        return {**base, "outcome": "already_sent", "note": "this edition has already gone to this recipient"}
    if state == "needs_review":
        return {**base, "outcome": "needs_review",
                "note": "an earlier attempt ended without a clear answer; a person must resolve it before "
                        "this recipient is contacted again about this edition"}

    row = ledger.get(did)
    attempts = row["attempts"] if row else 0
    max_attempts = int((cfg.get("retry") or {}).get("max_attempts", 3))
    if attempts >= max_attempts:
        return {**base, "outcome": "attempts_exhausted", "attempts": attempts,
                "note": f"{attempts} attempts already made; not tried again automatically"}

    if dry_run or not cfg.get("enabled"):
        reason = "dry run" if dry_run else "delivery is disabled in config/delivery.yaml"
        ledger.mark_skipped(did, reason)
        return {**base, "outcome": "not_sent", "reason": reason,
                "note": "the message was composed and recorded; nothing was transmitted"}

    missing = provider.missing_credentials()
    if missing:
        reason = f"missing credentials: {', '.join(missing)}"
        ledger.mark_skipped(did, reason)
        return {**base, "outcome": "not_sent", "reason": reason}

    ledger.mark_sending(did)
    if channel == "email":
        result = provider.send(to=person.address, subject=message.subject, body_text=message.body_text,
                               body_html=message.body_html, attachment=message.attachment)
    else:
        result = provider.send_template(
            to=person.address, template_key="report_ready",
            variables={"report_name": _report_name(report_type), "edition": edition,
                       "location_hint": "the usual archive"})

    if result.outcome == "sent":
        ledger.mark_sent(did, result.message_id, result.response)
        return {**base, "outcome": "sent", "provider_message_id": result.message_id}
    if result.outcome == "ambiguous":
        ledger.mark_needs_review(did, result.error or "ambiguous provider response", result.response)
        return {**base, "outcome": "needs_review", "error": result.error,
                "note": "the provider's answer did not establish whether the message went out; it will not be "
                        "retried automatically"}
    retry_at = _retry_at(attempts + 1, cfg.get("retry") or {})
    ledger.mark_failed(did, result.error or "send failed", retry_at, result.response)
    return {**base, "outcome": "failed", "error": result.error, "next_attempt_at": retry_at}


def _report_name(report_type: str) -> str:
    return {"monthly": "Macro & Banking Monitor", "weekly": "Weekly release digest",
            "sector": "Sector review", "mpr_brief": "Monetary Policy Review brief",
            "fsr_brief": "Financial Stability Report brief",
            "decision_update": "Policy decision update"}.get(report_type, report_type)


def send_alert(severity: str, headline: str, detail: str, *, dry_run: bool = False,
               ledger: DeliveryLedger | None = None) -> dict[str, Any]:
    """An operational alert to the owner. Never carries report content."""
    cfg = config.delivery_config()
    route = (cfg.get("routing") or {}).get("alert") or {}
    own = ledger is None
    ledger = ledger or DeliveryLedger(ledger_path())
    out: dict[str, Any] = {"severity": severity, "headline": headline, "deliveries": []}
    try:
        ch_cfg = (cfg.get("channels") or {}).get("email") or {}
        if not ch_cfg.get("enabled"):
            out["note"] = "the email channel is disabled; the alert is recorded in the run summary only"
            return out
        people, skipped = recipients_for("alert", "email")
        out["skipped"] = skipped
        message = composer.compose_alert(severity, headline, detail,
                                         subject_template=route.get("subject", "[azmonitor] {severity}: {headline}"),
                                         footer=(ch_cfg.get("message") or {}).get("footer", ""))
        provider = P.email_provider(ch_cfg)
        for person in people:
            did, state = ledger.enqueue(report_type="alert", edition=message.edition, version=None,
                                        channel="email", recipient_id=person.id,
                                        recipient_address=person.address, provider=provider.name,
                                        subject=message.subject, content_hash=message.content_hash)
            if state in ("sent", "needs_review"):
                out["deliveries"].append({"delivery_id": did, "outcome": state})
                continue
            if dry_run or not cfg.get("enabled"):
                ledger.mark_skipped(did, "dry run" if dry_run else "delivery is disabled")
                out["deliveries"].append({"delivery_id": did, "outcome": "not_sent"})
                continue
            missing = provider.missing_credentials()
            if missing:
                ledger.mark_skipped(did, f"missing credentials: {', '.join(missing)}")
                out["deliveries"].append({"delivery_id": did, "outcome": "not_sent",
                                          "reason": f"missing credentials: {', '.join(missing)}"})
                continue
            ledger.mark_sending(did)
            res = provider.send(to=person.address, subject=message.subject, body_text=message.body_text,
                                body_html=message.body_html, attachment=None)
            if res.outcome == "sent":
                ledger.mark_sent(did, res.message_id, res.response)
            elif res.outcome == "ambiguous":
                ledger.mark_needs_review(did, res.error or "ambiguous", res.response)
            else:
                ledger.mark_failed(did, res.error or "failed", None, res.response)
            out["deliveries"].append({"delivery_id": did, "outcome": res.outcome})
    finally:
        if own:
            ledger.close()
    return out


def status(limit: int = 20) -> dict[str, Any]:
    """What the ledger holds: counts, anything awaiting a person, anything still retryable."""
    led = DeliveryLedger(ledger_path())
    try:
        review = [dict(r) for r in led.needing_review()]
        due = [dict(r) for r in led.due()]
        recent = [dict(r) for r in led.conn.execute(
            "SELECT * FROM deliveries ORDER BY updated_at DESC LIMIT ?", (limit,)).fetchall()]
        return {"counts": led.summary(),
                "needs_review": [{k: r[k] for k in ("delivery_id", "report_type", "edition", "channel",
                                                    "recipient_hint", "last_error", "updated_at")} for r in review],
                "retryable": [{k: r[k] for k in ("delivery_id", "report_type", "edition", "attempts",
                                                 "next_attempt_at", "last_error")} for r in due],
                "recent": [{k: r[k] for k in ("delivery_id", "report_type", "edition", "channel", "status",
                                              "attempts", "updated_at")} for r in recent]}
    finally:
        led.close()
