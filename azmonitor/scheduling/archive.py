"""Keeping the report archive a useful size without losing anything that matters.

An edition directory is immutable once written, which makes retention simple but also makes it easy
to accumulate for ever: every forced regeneration during a week of development leaves a version
behind. Two rules keep it bounded:

* keep the last N versions of each edition — earlier ones were superseded within the same edition;
* keep the last N editions of each report type — older ones are history, and the dataset they were
  built from is still there.

Two things are never removed, whatever the limits say:

* the version `outputs/latest/<type>` points at, because a link in a delivered email resolves to it;
* any version that has been **delivered to someone**, because a recipient holding a PDF and finding
  the archive has forgotten it is worse than a large disk.

Nothing here runs automatically on a schedule by default: pruning is a decision, and
`monitor archive prune` makes it on request, with `--dry-run` to see it first.
"""
from __future__ import annotations

import datetime as dt
import json
import shutil
from pathlib import Path
from typing import Any

from .. import config
from ..util.log import get_logger

log = get_logger("archive")

VERSION_DIR = "v"          # every edition version directory is v<N>_<timestamp>


def _version_number(path: Path) -> int:
    try:
        return int(path.name.split("_")[0].lstrip("v"))
    except (ValueError, IndexError):
        return 0


def _versions(edition_dir: Path) -> list[Path]:
    """Version directories of one edition, oldest first.

    Sorted by version *number*, not by name: `v9` sorts after `v36` as a string, which would make
    retention keep the wrong twelve and an index report the wrong latest.
    """
    out = [p for p in edition_dir.iterdir() if p.is_dir() and p.name.startswith(VERSION_DIR)]
    return sorted(out, key=lambda p: (_version_number(p), p.name))


def _protected_paths() -> set[Path]:
    """Everything a `latest` pointer resolves to. These are never candidates for removal."""
    out: set[Path] = set()
    latest = config.paths().output_dir / "latest"
    if not latest.exists():
        return out
    for link in latest.iterdir():
        try:
            if link.is_symlink() or link.is_dir():
                out.add(link.resolve())
        except OSError:
            continue
    return out


def _delivered_paths() -> set[str]:
    """Editions that have gone to someone, by (report_type, edition, version).

    A delivered report is a promise: the recipient has the PDF and may come looking for the workbook.
    Retention does not get to break that, so these are kept regardless of age.
    """
    from ..delivery.dispatch import ledger_path

    path = ledger_path()
    if not path.exists():
        return set()
    from ..delivery.records import DeliveryLedger

    led = DeliveryLedger(path)
    try:
        rows = led.conn.execute(
            "SELECT DISTINCT report_type, edition, version FROM deliveries WHERE status='sent'").fetchall()
        return {f"{r['report_type']}|{r['edition']}|{r['version']}" for r in rows}
    finally:
        led.close()


def plan(now: dt.date | None = None) -> dict[str, Any]:
    """What pruning would remove, and what it would keep and why."""
    cfg = config.schedule_config().get("archive") or {}
    keep_versions = int(cfg.get("keep_versions_per_edition", 12))
    keep_editions = cfg.get("keep_editions") or {}
    out_dir = config.paths().output_dir
    protected = _protected_paths()
    delivered = _delivered_paths()

    remove: list[dict[str, Any]] = []
    keep_reasons: list[dict[str, Any]] = []
    total_bytes = 0

    for type_dir in sorted(p for p in out_dir.iterdir() if p.is_dir() and p.name != "latest"):
        report_type = type_dir.name
        editions = sorted((p for p in type_dir.iterdir() if p.is_dir()), key=lambda p: p.name)
        limit_editions = int(keep_editions.get(report_type, 0) or 0)

        old_editions = editions[:-limit_editions] if limit_editions and len(editions) > limit_editions else []
        for edition_dir in editions:
            versions = _versions(edition_dir)
            doomed: list[Path] = []
            if edition_dir in old_editions:
                doomed = list(versions)
            elif len(versions) > keep_versions:
                doomed = versions[:-keep_versions]

            for v in doomed:
                key = f"{report_type}|{edition_dir.name}|{_version_number(v)}"
                if v.resolve() in protected:
                    keep_reasons.append({"path": str(v), "reason": "outputs/latest points at it"})
                    continue
                if key in delivered:
                    keep_reasons.append({"path": str(v), "reason": "this version was delivered to a recipient"})
                    continue
                size = sum(f.stat().st_size for f in v.rglob("*") if f.is_file())
                total_bytes += size
                remove.append({"path": str(v), "report_type": report_type, "edition": edition_dir.name,
                               "version": _version_number(v), "bytes": size,
                               "reason": ("the edition is older than the retention for this report type"
                                          if edition_dir in old_editions
                                          else f"superseded within its edition; only the last {keep_versions} "
                                               f"versions are kept")})

    return {"would_remove": remove, "kept_despite_limits": keep_reasons,
            "bytes_freed": total_bytes, "mb_freed": round(total_bytes / (1024 * 1024), 1),
            "limits": {"versions_per_edition": keep_versions, "editions": keep_editions}}


def prune(dry_run: bool = True) -> dict[str, Any]:
    """Apply the retention rules. Defaults to a dry run because deleting reports is not reversible."""
    p = plan()
    if dry_run:
        p["applied"] = False
        return p
    removed = []
    for item in p["would_remove"]:
        try:
            shutil.rmtree(item["path"])
            removed.append(item["path"])
        except OSError as exc:
            log.warning("could not remove %s: %s", item["path"], exc)
            item["error"] = str(exc)
    p["applied"] = True
    p["removed"] = removed
    log.info("archive pruned: %d version(s) removed, %.1f MB freed", len(removed), p["mb_freed"])
    return p


def index() -> dict[str, Any]:
    """What the archive holds, for a status view or for serving a listing."""
    out_dir = config.paths().output_dir
    if not out_dir.exists():
        return {"report_types": {}, "total_editions": 0, "total_versions": 0}
    types: dict[str, Any] = {}
    n_editions = n_versions = 0
    for type_dir in sorted(p for p in out_dir.iterdir() if p.is_dir() and p.name != "latest"):
        editions = []
        for edition_dir in sorted((p for p in type_dir.iterdir() if p.is_dir()), key=lambda p: p.name):
            versions = _versions(edition_dir)
            if not versions:
                continue
            latest = versions[-1]
            manifest = latest / "manifest.json"
            meta: dict[str, Any] = {}
            if manifest.exists():
                try:
                    m = json.loads(manifest.read_text(encoding="utf-8"))
                    meta = {"generated_at": m.get("generated_at"), "n_slides": m.get("n_slides"),
                            "status_label": m.get("status_label"),
                            "fingerprint": (m.get("edition_fingerprint") or {}).get("fingerprint")}
                except ValueError:
                    meta = {"note": "the manifest could not be read"}
            editions.append({"edition": edition_dir.name, "versions": len(versions),
                             "latest_version": _version_number(latest), "path": str(latest), **meta})
            n_editions += 1
            n_versions += len(versions)
        if editions:
            types[type_dir.name] = editions
    return {"report_types": types, "total_editions": n_editions, "total_versions": n_versions}
