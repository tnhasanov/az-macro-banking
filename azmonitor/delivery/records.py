"""The record of what was sent, to whom, and what the provider said about it.

A board pack delivered twice is worse than one delivered late, so the ledger is written *before* a
provider is called and updated after, never the other way round. That ordering is what makes the
interesting case survivable: the process dies between the send and the acknowledgement. On restart
the row is still there, marked `sending`, and the next run can see that something was attempted and
refuse to attempt it again on its own.

A provider call has three outcomes, not two:

* **sent** — the provider acknowledged it, with an id. Never sent again.
* **failed** — the provider refused it, clearly. Retryable, within the configured attempts.
* **needs_review** — the answer was ambiguous: a timeout, a dropped connection, a 5xx after the
  request was accepted, a success with no message id. The message may or may not have gone out. This
  is never retried automatically; a person decides, and `monitor delivery resolve` records what they
  decided. Treating ambiguity as failure is how a recipient gets the same report twice.

Identity is (edition, channel, recipient). The same edition re-delivered to the same person over the
same channel is the same delivery, whatever run produced it, so a rerun of an unchanged edition adds
nothing and sends nothing.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any

from ..util.log import get_logger

log = get_logger("delivery")

SCHEMA = """
CREATE TABLE IF NOT EXISTS deliveries (
  delivery_id       TEXT PRIMARY KEY,   -- deterministic: report + edition + version + channel + recipient
  report_type       TEXT NOT NULL,
  edition           TEXT NOT NULL,      -- the edition key, e.g. 2026-07 or a publication id
  version           INTEGER,            -- the edition version actually delivered
  channel           TEXT NOT NULL,      -- email | whatsapp
  recipient_id      TEXT NOT NULL,      -- the id from the distribution list, not the address
  recipient_hint    TEXT,               -- masked address, enough to identify it in a log
  status            TEXT NOT NULL,      -- pending | sending | sent | failed | needs_review | skipped
  attempts          INTEGER NOT NULL DEFAULT 0,
  provider          TEXT,
  provider_message_id TEXT,
  subject           TEXT,
  content_hash      TEXT,               -- what was composed, so a changed message is a new delivery
  artifact_paths    TEXT,               -- JSON: what was attached and what was linked
  created_at        TEXT NOT NULL,
  updated_at        TEXT NOT NULL,
  sent_at           TEXT,
  next_attempt_at   TEXT,
  last_error        TEXT,
  last_response     TEXT,               -- trimmed provider response, for the ambiguous cases
  resolution        TEXT,               -- how a person resolved a needs_review row
  resolved_by       TEXT,
  resolved_at       TEXT
);
CREATE INDEX IF NOT EXISTS ix_deliveries_edition ON deliveries(report_type, edition, status);
CREATE INDEX IF NOT EXISTS ix_deliveries_status ON deliveries(status, next_attempt_at);
"""

# Only a confirmed send is final. A skip is not: "delivery was disabled" and "this was a dry run"
# are statements about a moment, not about the edition, and a row skipped for either reason must
# still be deliverable once the channel is switched on.
TERMINAL = ("sent",)
AMBIGUOUS = "needs_review"


def utcnow() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def mask(address: str | None) -> str:
    """Enough of an address to recognise it in a log, not enough to be a contact list."""
    if not address:
        return ""
    if "@" in address:
        user, _, domain = address.partition("@")
        head = user[:2] if len(user) > 3 else user[:1]
        return f"{head}{'*' * max(3, len(user) - len(head))}@{domain}"
    digits = "".join(ch for ch in address if ch.isdigit())
    return f"{'*' * max(0, len(digits) - 4)}{digits[-4:]}" if digits else "****"


def delivery_id(report_type: str, edition: str, version: int | None, channel: str, recipient_id: str) -> str:
    """Deterministic identity, so the same delivery cannot be created twice under two names."""
    key = f"{report_type}|{edition}|{version if version is not None else '-'}|{channel}|{recipient_id}"
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:24]


class DeliveryLedger:
    """Delivery records in their own database, beside the dataset but not inside it.

    Kept separate on purpose: the dataset is rebuildable from the sources at any time, and rebuilding
    it must not erase the record of what has already gone out to people.
    """

    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    # ------------------------------------------------------------------ reading
    def get(self, did: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM deliveries WHERE delivery_id=?", (did,)).fetchone()

    def for_edition(self, report_type: str, edition: str) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM deliveries WHERE report_type=? AND edition=? ORDER BY created_at",
            (report_type, edition)).fetchall()

    def due(self, now: str | None = None) -> list[sqlite3.Row]:
        """Rows a run may act on: never sent, never ambiguous, and past their backoff."""
        now = now or utcnow()
        return self.conn.execute(
            "SELECT * FROM deliveries WHERE status IN ('pending','failed') "
            "AND (next_attempt_at IS NULL OR next_attempt_at <= ?) ORDER BY created_at", (now,)).fetchall()

    def needing_review(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM deliveries WHERE status=? AND resolution IS NULL ORDER BY created_at",
            (AMBIGUOUS,)).fetchall()

    def stuck(self, older_than_minutes: int, now: dt.datetime | None = None) -> list[sqlite3.Row]:
        """Rows left mid-flight: a process died between writing `sending` and hearing back."""
        cut = ((now or dt.datetime.now(dt.timezone.utc)) - dt.timedelta(minutes=older_than_minutes)).isoformat()
        return self.conn.execute(
            "SELECT * FROM deliveries WHERE status='sending' AND updated_at <= ? ORDER BY updated_at", (cut,)).fetchall()

    def summary(self) -> dict[str, int]:
        rows = self.conn.execute("SELECT status, COUNT(*) n FROM deliveries GROUP BY status").fetchall()
        return {r["status"]: r["n"] for r in rows}

    # ------------------------------------------------------------------ writing
    def enqueue(self, *, report_type: str, edition: str, version: int | None, channel: str, recipient_id: str,
                recipient_address: str | None, provider: str, subject: str, content_hash: str,
                artifacts: dict[str, Any] | None = None) -> tuple[str, str]:
        """Record the intent to send. Returns (delivery_id, state).

        `state` is "new" when this run created the row, or the existing status when it did not. An
        already-sent delivery returns "sent" and the caller sends nothing: this is the check that
        makes a rerun of an unchanged edition a no-op.
        """
        did = delivery_id(report_type, edition, version, channel, recipient_id)
        row = self.get(did)
        if row is not None:
            # A message whose content changed is a different delivery: a corrected edition should go
            # out, and it carries its own version, so this only catches a genuine re-send attempt.
            if row["status"] in TERMINAL:
                return did, row["status"]
            if row["status"] == AMBIGUOUS:
                return did, AMBIGUOUS
            if row["status"] == "skipped":
                # a dry run or a disabled channel recorded the intent; the send is still owed
                self.conn.execute(
                    "UPDATE deliveries SET status='pending', content_hash=?, subject=?, last_error=NULL, "
                    "updated_at=? WHERE delivery_id=?", (content_hash, subject, utcnow(), did))
                self.conn.commit()
                return did, "retry_after_skip"
            return did, row["status"]
        now = utcnow()
        self.conn.execute(
            "INSERT INTO deliveries (delivery_id, report_type, edition, version, channel, recipient_id, "
            "recipient_hint, status, attempts, provider, subject, content_hash, artifact_paths, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,'pending',0,?,?,?,?,?,?)",
            (did, report_type, edition, version, channel, recipient_id, mask(recipient_address), provider,
             subject, content_hash, json.dumps(artifacts or {}, ensure_ascii=False), now, now))
        self.conn.commit()
        return did, "new"

    def mark_sending(self, did: str) -> None:
        """Written before the provider is called, so a crash mid-call leaves a trace."""
        self.conn.execute(
            "UPDATE deliveries SET status='sending', attempts=attempts+1, updated_at=? WHERE delivery_id=?",
            (utcnow(), did))
        self.conn.commit()

    def mark_sent(self, did: str, message_id: str | None, response: str | None = None) -> None:
        now = utcnow()
        self.conn.execute(
            "UPDATE deliveries SET status='sent', provider_message_id=?, sent_at=?, updated_at=?, "
            "last_error=NULL, last_response=? WHERE delivery_id=?",
            (message_id, now, now, (response or "")[:1000], did))
        self.conn.commit()

    def mark_failed(self, did: str, error: str, retry_at: str | None, response: str | None = None) -> None:
        self.conn.execute(
            "UPDATE deliveries SET status='failed', last_error=?, next_attempt_at=?, updated_at=?, last_response=? "
            "WHERE delivery_id=?", (error[:500], retry_at, utcnow(), (response or "")[:1000], did))
        self.conn.commit()

    def mark_needs_review(self, did: str, error: str, response: str | None = None) -> None:
        """The provider's answer did not establish whether the message went out.

        This is deliberately a dead end for automation. Nothing retries it, nothing expires it, and
        it stays visible in `monitor delivery status` until a person says what happened.
        """
        self.conn.execute(
            "UPDATE deliveries SET status=?, last_error=?, next_attempt_at=NULL, updated_at=?, last_response=? "
            "WHERE delivery_id=?", (AMBIGUOUS, error[:500], utcnow(), (response or "")[:1000], did))
        self.conn.commit()
        log.warning("delivery %s needs review: %s", did, error[:200])

    def mark_skipped(self, did: str, reason: str) -> None:
        self.conn.execute(
            "UPDATE deliveries SET status='skipped', last_error=?, updated_at=? WHERE delivery_id=?",
            (reason[:500], utcnow(), did))
        self.conn.commit()

    def resolve(self, did: str, resolution: str, resolved_by: str) -> bool:
        """Record what a person decided about an ambiguous delivery.

        `resolution` is "delivered" (it did go out; nothing more to do) or "not_delivered" (it did
        not; the row returns to the queue and may be attempted again).
        """
        if resolution not in ("delivered", "not_delivered"):
            raise ValueError("resolution must be 'delivered' or 'not_delivered'")
        row = self.get(did)
        if row is None or row["status"] != AMBIGUOUS:
            return False
        status = "sent" if resolution == "delivered" else "pending"
        now = utcnow()
        self.conn.execute(
            "UPDATE deliveries SET status=?, resolution=?, resolved_by=?, resolved_at=?, updated_at=?, "
            "next_attempt_at=NULL, sent_at=COALESCE(sent_at, ?) WHERE delivery_id=?",
            (status, resolution, resolved_by, now, now, now if status == "sent" else None, did))
        self.conn.commit()
        return True

    def recover_stuck(self, older_than_minutes: int) -> list[str]:
        """A row left in `sending` by a dead process is ambiguous, not failed.

        The process died somewhere between calling the provider and hearing back, and there is no
        way from here to know which side of the send that was. It goes to a person.
        """
        out = []
        for row in self.stuck(older_than_minutes):
            self.mark_needs_review(row["delivery_id"],
                                   "the run ended while this send was in flight; whether it reached the provider "
                                   "was never established")
            out.append(row["delivery_id"])
        return out
