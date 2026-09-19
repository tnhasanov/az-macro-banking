"""SQLite persistence: documents, append-only observation vintages, dataset state, runs, editions.

Design notes
- Observations are append-only. A replacement file that changes a value for an existing
  (series, dims, period) inserts a new row in a new vintage and marks the old row
  `superseded`; unchanged values create no new rows (idempotent re-runs).
- Publication dates are stored only when explicitly available (title date, HTTP
  Last-Modified or a date printed in the document). `published_at_basis` says which.
"""
from __future__ import annotations

import datetime as dt
import json
import sqlite3
import uuid
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path
from typing import Any, Iterable

from ..parsers.base import Observation

SCHEMA = """
CREATE TABLE IF NOT EXISTS documents (
  doc_id TEXT PRIMARY KEY,
  source_id TEXT NOT NULL,
  dataset_id TEXT NOT NULL,
  discovery_url TEXT,
  document_url TEXT NOT NULL,
  title_original TEXT,
  title_en TEXT,
  content_type TEXT,
  sha256 TEXT NOT NULL,
  size_bytes INTEGER,
  stored_path TEXT,
  retrieved_at TEXT NOT NULL,
  first_seen_at TEXT NOT NULL,
  published_at TEXT,
  published_at_basis TEXT,
  http_last_modified TEXT,
  status TEXT NOT NULL DEFAULT 'downloaded',
  note TEXT
);
CREATE INDEX IF NOT EXISTS ix_documents_dataset ON documents(dataset_id, retrieved_at);

CREATE TABLE IF NOT EXISTS document_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  doc_id TEXT,
  dataset_id TEXT,
  event TEXT NOT NULL,
  at TEXT NOT NULL,
  detail TEXT
);

CREATE TABLE IF NOT EXISTS vintages (
  vintage_id TEXT PRIMARY KEY,
  dataset_id TEXT NOT NULL,
  doc_id TEXT NOT NULL,
  created_at TEXT NOT NULL,
  n_obs INTEGER, n_new_periods INTEGER, n_revisions INTEGER, n_unchanged INTEGER,
  revision_summary TEXT
);

CREATE TABLE IF NOT EXISTS observations (
  obs_id INTEGER PRIMARY KEY AUTOINCREMENT,
  series_id TEXT NOT NULL,
  dims TEXT NOT NULL DEFAULT '{}',
  period_start TEXT,
  period_end TEXT NOT NULL,
  freq TEXT,
  period_type TEXT,
  value REAL,
  value_raw TEXT,
  missing_reason TEXT,
  unit TEXT,
  scale_note TEXT,
  currency TEXT,
  population TEXT,
  basis TEXT,
  source_id TEXT,
  dataset_id TEXT,
  doc_id TEXT,
  sheet TEXT,
  cell_ref TEXT,
  label_original TEXT,
  extraction_method TEXT,
  vintage_id TEXT,
  first_observed_at TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'current',
  superseded_at TEXT,
  preliminary INTEGER DEFAULT 0,
  method_break INTEGER DEFAULT 0,
  flags TEXT,
  published_at TEXT
);
CREATE INDEX IF NOT EXISTS ix_obs_series ON observations(series_id, dims, period_end, status);
CREATE INDEX IF NOT EXISTS ix_obs_dataset ON observations(dataset_id, status);

CREATE TABLE IF NOT EXISTS dataset_state (
  dataset_id TEXT PRIMARY KEY,
  source_id TEXT,
  last_doc_id TEXT,
  last_sha256 TEXT,
  last_checked_at TEXT,
  last_changed_at TEXT,
  latest_period_end TEXT,
  status TEXT,
  message TEXT
);

CREATE TABLE IF NOT EXISTS discovered_links (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  source_id TEXT, dataset_id TEXT, discovery_url TEXT, document_url TEXT, title TEXT,
  size_text TEXT, published_at TEXT, published_at_basis TEXT, first_seen_at TEXT, last_seen_at TEXT,
  UNIQUE(source_id, document_url)
);

CREATE TABLE IF NOT EXISTS runs (
  run_id TEXT PRIMARY KEY,
  command TEXT, started_at TEXT, finished_at TEXT, status TEXT, summary TEXT
);

CREATE TABLE IF NOT EXISTS report_editions (
  edition_id TEXT PRIMARY KEY,
  report_type TEXT, edition_period TEXT, version INTEGER, generated_at TEXT, as_of TEXT,
  snapshot_id TEXT, status TEXT, path TEXT, manifest_path TEXT, anchors TEXT
);

CREATE TABLE IF NOT EXISTS series_registry (
  series_id TEXT PRIMARY KEY,
  dataset_id TEXT, source_id TEXT, label_en TEXT, label_az TEXT, unit TEXT, population TEXT,
  period_type TEXT, frequency TEXT
);
"""


def utcnow() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


class Database:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.executescript(SCHEMA)

    def close(self) -> None:
        self.conn.close()

    @contextmanager
    def tx(self):
        try:
            yield self.conn
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    # ---- documents -------------------------------------------------------------
    def get_document(self, doc_id: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM documents WHERE doc_id=?", (doc_id,)).fetchone()

    def find_document_by_sha(self, dataset_id: str, sha256: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM documents WHERE dataset_id=? AND sha256=? ORDER BY retrieved_at LIMIT 1", (dataset_id, sha256)
        ).fetchone()

    def upsert_document(self, rec: dict[str, Any]) -> None:
        cols = ",".join(rec.keys())
        placeholders = ",".join("?" for _ in rec)
        updates = ",".join(f"{k}=excluded.{k}" for k in rec if k not in ("doc_id", "first_seen_at"))
        with self.tx() as c:
            c.execute(f"INSERT INTO documents ({cols}) VALUES ({placeholders}) ON CONFLICT(doc_id) DO UPDATE SET {updates}", tuple(rec.values()))

    def log_event(self, event: str, doc_id: str | None, dataset_id: str | None, detail: Any = None) -> None:
        with self.tx() as c:
            c.execute(
                "INSERT INTO document_events(doc_id, dataset_id, event, at, detail) VALUES (?,?,?,?,?)",
                (doc_id, dataset_id, event, utcnow(), json.dumps(detail, ensure_ascii=False, default=str) if detail is not None else None),
            )

    def documents_for_dataset(self, dataset_id: str) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM documents WHERE dataset_id=? ORDER BY retrieved_at", (dataset_id,)).fetchall()

    def all_documents(self) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM documents ORDER BY source_id, dataset_id, retrieved_at").fetchall()

    def record_link(self, rec: dict[str, Any]) -> None:
        now = utcnow()
        with self.tx() as c:
            c.execute(
                """INSERT INTO discovered_links(source_id, dataset_id, discovery_url, document_url, title, size_text, published_at, published_at_basis, first_seen_at, last_seen_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(source_id, document_url) DO UPDATE SET last_seen_at=excluded.last_seen_at, title=excluded.title, dataset_id=excluded.dataset_id""",
                (rec["source_id"], rec.get("dataset_id"), rec.get("discovery_url"), rec["document_url"], rec.get("title"), rec.get("size_text"),
                 rec.get("published_at"), rec.get("published_at_basis"), now, now),
            )

    # ---- observations ----------------------------------------------------------
    def current_observations(self, series_ids: Iterable[str] | None = None, dataset_id: str | None = None) -> list[sqlite3.Row]:
        sql = "SELECT * FROM observations WHERE status='current'"
        params: list[Any] = []
        if series_ids is not None:
            ids = list(series_ids)
            sql += f" AND series_id IN ({','.join('?' for _ in ids)})"
            params += ids
        if dataset_id is not None:
            sql += " AND dataset_id=?"
            params.append(dataset_id)
        sql += " ORDER BY series_id, dims, period_end"
        return self.conn.execute(sql, params).fetchall()

    def observations_as_of(self, cutoff_iso: str) -> list[sqlite3.Row]:
        """Historical information set: rows first observed on or before the cutoff and
        not superseded before it. Genuine only where the stored vintages cover the cutoff."""
        return self.conn.execute(
            "SELECT * FROM observations WHERE first_observed_at<=? AND (superseded_at IS NULL OR superseded_at>?) ORDER BY series_id, dims, period_end",
            (cutoff_iso, cutoff_iso),
        ).fetchall()

    def store_observations(self, dataset_id: str, doc_id: str, obs: list[Observation], materiality: float = 1e-9,
                           published_at: str | None = None) -> dict[str, Any]:
        """Append-only vintage insert. Returns change statistics."""
        now = utcnow()
        vintage_id = f"{dataset_id}:{doc_id.split(':')[-1][:12]}:{now}:{uuid.uuid4().hex[:6]}"
        n_new = n_rev = n_same = n_missing = 0
        revisions: list[dict[str, Any]] = []
        with self.tx() as c:
            for o in obs:
                dims = json.dumps(o.dims, ensure_ascii=False, sort_keys=True)
                cur = c.execute(
                    "SELECT obs_id, value, missing_reason FROM observations WHERE series_id=? AND dims=? AND period_end=? AND status='current'",
                    (o.series_id, dims, o.period_end.isoformat()),
                ).fetchone()
                if cur is not None:
                    same_value = (cur["value"] is None and o.value is None) or (
                        cur["value"] is not None and o.value is not None and abs(cur["value"] - o.value) <= materiality
                    )
                    if same_value:
                        n_same += 1
                        continue
                    if cur["value"] is not None and o.value is None:
                        # Do not let a missing marker overwrite a published value silently.
                        n_missing += 1
                        continue
                    c.execute("UPDATE observations SET status='superseded', superseded_at=? WHERE obs_id=?", (now, cur["obs_id"]))
                    n_rev += 1
                    revisions.append({"series_id": o.series_id, "dims": o.dims, "period_end": o.period_end.isoformat(),
                                      "old": cur["value"], "new": o.value})
                else:
                    n_new += 1
                c.execute(
                    """INSERT INTO observations(series_id, dims, period_start, period_end, freq, period_type, value, value_raw, missing_reason,
                       unit, scale_note, currency, population, basis, source_id, dataset_id, doc_id, sheet, cell_ref, label_original,
                       extraction_method, vintage_id, first_observed_at, status, preliminary, method_break, flags, published_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'current',?,?,?,?)""",
                    (o.series_id, dims, o.period_start.isoformat() if o.period_start else None, o.period_end.isoformat(), o.freq, o.period_type,
                     o.value, o.value_raw, o.missing_reason, o.unit, o.scale_note, o.currency, o.population, o.basis, o.source_id, dataset_id,
                     doc_id, o.sheet, o.cell_ref, o.label_original, o.extraction_method, vintage_id, now, int(o.preliminary), int(o.method_break),
                     json.dumps(o.flags) if o.flags else None, published_at),
                )
            if n_new or n_rev:
                c.execute(
                    "INSERT INTO vintages(vintage_id, dataset_id, doc_id, created_at, n_obs, n_new_periods, n_revisions, n_unchanged, revision_summary) VALUES (?,?,?,?,?,?,?,?,?)",
                    (vintage_id, dataset_id, doc_id, now, len(obs), n_new, n_rev, n_same, json.dumps(revisions[:200], ensure_ascii=False)),
                )
        return {"vintage_id": vintage_id if (n_new or n_rev) else None, "n_obs": len(obs), "n_new": n_new, "n_revisions": n_rev,
                "n_unchanged": n_same, "n_missing_not_applied": n_missing, "revisions": revisions}

    # ---- state -----------------------------------------------------------------
    def set_dataset_state(self, dataset_id: str, **fields: Any) -> None:
        cur = self.conn.execute("SELECT * FROM dataset_state WHERE dataset_id=?", (dataset_id,)).fetchone()
        rec = dict(cur) if cur else {"dataset_id": dataset_id}
        rec.update({k: v for k, v in fields.items()})
        cols = ",".join(rec.keys())
        placeholders = ",".join("?" for _ in rec)
        updates = ",".join(f"{k}=excluded.{k}" for k in rec if k != "dataset_id")
        with self.tx() as c:
            c.execute(f"INSERT INTO dataset_state ({cols}) VALUES ({placeholders}) ON CONFLICT(dataset_id) DO UPDATE SET {updates}", tuple(rec.values()))

    def dataset_states(self) -> dict[str, sqlite3.Row]:
        return {r["dataset_id"]: r for r in self.conn.execute("SELECT * FROM dataset_state").fetchall()}

    def register_series(self, rec: dict[str, Any]) -> None:
        cols = ",".join(rec.keys())
        placeholders = ",".join("?" for _ in rec)
        updates = ",".join(f"{k}=excluded.{k}" for k in rec if k != "series_id")
        with self.tx() as c:
            c.execute(f"INSERT INTO series_registry ({cols}) VALUES ({placeholders}) ON CONFLICT(series_id) DO UPDATE SET {updates}", tuple(rec.values()))

    def series_registry(self) -> dict[str, sqlite3.Row]:
        return {r["series_id"]: r for r in self.conn.execute("SELECT * FROM series_registry").fetchall()}

    def start_run(self, run_id: str, command: str) -> None:
        with self.tx() as c:
            c.execute("INSERT OR REPLACE INTO runs(run_id, command, started_at, status) VALUES (?,?,?,?)", (run_id, command, utcnow(), "running"))

    def finish_run(self, run_id: str, status: str, summary: Any) -> None:
        with self.tx() as c:
            c.execute("UPDATE runs SET finished_at=?, status=?, summary=? WHERE run_id=?", (utcnow(), status, json.dumps(summary, ensure_ascii=False, default=str), run_id))

    def add_edition(self, rec: dict[str, Any]) -> None:
        cols = ",".join(rec.keys())
        placeholders = ",".join("?" for _ in rec)
        with self.tx() as c:
            c.execute(f"INSERT OR REPLACE INTO report_editions ({cols}) VALUES ({placeholders})", tuple(rec.values()))

    def editions(self, report_type: str | None = None) -> list[sqlite3.Row]:
        if report_type:
            return self.conn.execute("SELECT * FROM report_editions WHERE report_type=? ORDER BY generated_at", (report_type,)).fetchall()
        return self.conn.execute("SELECT * FROM report_editions ORDER BY generated_at").fetchall()

    def vintages_since(self, iso: str) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM vintages WHERE created_at>? ORDER BY created_at", (iso,)).fetchall()

    def all_vintages(self) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM vintages ORDER BY created_at").fetchall()


def observation_to_dict(o: Observation) -> dict[str, Any]:
    d = asdict(o)
    d["period_end"] = o.period_end.isoformat()
    d["period_start"] = o.period_start.isoformat() if o.period_start else None
    return d
