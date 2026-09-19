"""Safe backup and restore of the persistent dataset.

Copying a live SQLite file is not a backup: with write-ahead logging the file on disk can be missing
the most recent transactions, and a copy taken mid-write is torn. `VACUUM INTO` writes a consistent
snapshot of the database through SQLite itself, so the result opens cleanly even if a write was in
flight.

A restore is never trusted on arrival either. The restored file is opened, checked for integrity and
compared against the row counts recorded with the backup before the run is allowed to continue; a
restore that fails any of that stops the run rather than letting it rebuild history from an empty
database and publish a report from it.
"""
from __future__ import annotations

import datetime as dt
import json
import shutil
import sqlite3
from pathlib import Path
from typing import Any

from ..util.log import get_logger

log = get_logger("backup")

COUNTED_TABLES = ("documents", "observations", "vintages", "publications", "passages", "policy_decisions",
                  "report_editions")


def table_counts(path: Path) -> dict[str, int]:
    out: dict[str, int] = {}
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        names = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for t in COUNTED_TABLES:
            if t in names:
                out[t] = con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
    finally:
        con.close()
    return out


def integrity_ok(path: Path) -> tuple[bool, str]:
    if not path.exists():
        return False, "database file does not exist"
    try:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        return False, f"cannot open: {exc}"
    try:
        result = con.execute("PRAGMA integrity_check").fetchone()[0]
        return (result == "ok"), result
    except sqlite3.DatabaseError as exc:
        return False, f"integrity check failed: {exc}"
    finally:
        con.close()


def backup_database(db_path: Path, dest_dir: Path, keep: int = 7) -> dict[str, Any]:
    """Write a consistent snapshot of the database and a manifest beside it."""
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    target = dest_dir / f"monitor-{stamp}.sqlite"
    con = sqlite3.connect(db_path)
    try:
        con.execute("VACUUM INTO ?", (str(target),))
    finally:
        con.close()
    ok, detail = integrity_ok(target)
    manifest = {"created_at": stamp, "source": str(db_path), "file": target.name, "bytes": target.stat().st_size,
                "integrity": detail, "counts": table_counts(target) if ok else {}}
    (dest_dir / f"monitor-{stamp}.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    (dest_dir / "latest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    if not ok:
        target.unlink(missing_ok=True)
        raise RuntimeError(f"backup failed its integrity check: {detail}")
    _prune(dest_dir, keep)
    log.info("database backup written to %s (%d bytes)", target, manifest["bytes"])
    return manifest


def _prune(dest_dir: Path, keep: int) -> None:
    backups = sorted(dest_dir.glob("monitor-*.sqlite"))
    for old in backups[:-keep] if keep > 0 else []:
        old.unlink(missing_ok=True)
        old.with_suffix(".json").unlink(missing_ok=True)


def restore_database(backup_file: Path, db_path: Path) -> dict[str, Any]:
    """Put a verified backup in place, keeping whatever was there until the new file passes."""
    ok, detail = integrity_ok(Path(backup_file))
    if not ok:
        raise RuntimeError(f"refusing to restore a database that fails its integrity check: {detail}")
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    previous = None
    if db_path.exists():
        previous = db_path.with_suffix(".sqlite.replaced")
        shutil.copy2(db_path, previous)
    shutil.copy2(backup_file, db_path)
    for suffix in ("-wal", "-shm"):
        stale = Path(str(db_path) + suffix)
        stale.unlink(missing_ok=True)
    ok, detail = integrity_ok(db_path)
    if not ok:
        if previous:
            shutil.copy2(previous, db_path)
        raise RuntimeError(f"restored database failed verification and the previous file was put back: {detail}")
    return {"restored_from": str(backup_file), "counts": table_counts(db_path), "integrity": detail}


def verify_dataset(db_path: Path, expect: dict[str, int] | None = None, min_documents: int = 1) -> dict[str, Any]:
    """Check that a dataset is usable before a run writes to it or publishes from it."""
    ok, detail = integrity_ok(Path(db_path))
    problems: list[str] = []
    if not ok:
        problems.append(f"integrity: {detail}")
        return {"ok": False, "problems": problems, "counts": {}}
    counts = table_counts(Path(db_path))
    if counts.get("documents", 0) < min_documents:
        problems.append(f"only {counts.get('documents', 0)} documents in the restored dataset; "
                        f"a run against an empty dataset would republish from nothing")
    for table, expected in (expect or {}).items():
        actual = counts.get(table, 0)
        if actual < expected:
            problems.append(f"{table}: {actual} rows restored, {expected} expected")
    return {"ok": not problems, "problems": problems, "counts": counts, "integrity": detail}
