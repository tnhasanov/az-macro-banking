"""The commands a cloud worker runs around the existing engine.

    python -m azmonitor.cloud.publish restore     # before the cycle: pull the dataset down
    python -m azmonitor.cloud.publish save        # after it: push the dataset and new reports up
    python -m azmonitor.cloud.publish readmodel   # rebuild what the dashboard reads
    python -m azmonitor.cloud.publish status      # what the store and the database currently hold

`restore` and `save` are what `AZMONITOR_RESTORE_CMD` and `AZMONITOR_SAVE_CMD` are set to, so the
engine's own lock still brackets the whole cycle and nothing about the pipeline changes. The other
two are called by the worker after a successful run.

Every command prints JSON and exits non-zero on failure, because its caller is a shell script in a
CI job and that is the only interface such a caller can act on.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import traceback
from pathlib import Path
from typing import Any

from .. import config
from ..util.log import get_logger, setup_logging
from . import objectstore as OS

log = get_logger("cloud.publish")


def _out(payload: dict[str, Any], code: int = 0) -> int:
    print(json.dumps(payload, indent=2, default=str))
    return code


def cmd_restore(args) -> int:
    paths = config.paths()
    paths.ensure()
    store = OS.store_from_env()
    result = OS.restore_dataset(paths.data_dir, store)
    result["contents"] = OS.dataset_summary(paths.data_dir)
    if not result.get("restored"):
        # A first run legitimately has nothing to restore. The engine's own verification decides
        # whether an empty dataset may proceed, so this is reported, not treated as a failure.
        result["note"] = ("nothing was restored; if this is not the first run, stop and find out why "
                          "the store is empty before letting a backfill overwrite it")
    return _out(result)


# An operator who genuinely owns the branded content can set this to override the profile check.
# Spelled out in full rather than made a boolean, so it cannot be turned on by an inherited `1`.
UPLOAD_OVERRIDE = "AZMONITOR_ALLOW_RESTRICTED_UPLOAD"
UPLOAD_OVERRIDE_VALUE = "i-own-this-content"


def _refuse_restricted_upload() -> dict[str, Any] | None:
    """Stop before uploading anything the active profile is not allowed to distribute.

    In a cloud deployment the destination is personal object storage, and the branded profile
    renders a specific bank's logo and brand colours onto every slide. Those are the bank's assets.
    Uploading them would move a third party's property onto infrastructure that is not theirs, and
    no amount of access control on the bucket makes that the operator's call to make.

    So the refusal is in the upload path rather than in a runbook: it fires whatever invoked `save`,
    including an unattended scheduled run, and it fires before the dataset is written rather than
    after the reports are already up.
    """
    active = config.profile()
    if active.get("external_upload"):
        return None
    if os.environ.get(UPLOAD_OVERRIDE) == UPLOAD_OVERRIDE_VALUE:
        log.warning("uploading under the %r profile because %s is set", active["name"],
                    UPLOAD_OVERRIDE)
        return None
    return {
        "uploaded": False,
        "profile": active["name"],
        "error": (
            f"the {active['name']!r} distribution profile does not permit external upload"
        ),
        "reason": active.get("note", "").strip(),
        "remedy": (
            f"Render and upload under a profile that permits it (AZMONITOR_PROFILE=neutral), or, "
            f"if you own this content and intend to publish it, set "
            f"{UPLOAD_OVERRIDE}={UPLOAD_OVERRIDE_VALUE}."
        ),
    }


def cmd_save(args) -> int:
    refusal = _refuse_restricted_upload()
    if refusal:
        log.error("%s", refusal["error"])
        return _out(refusal, 3)

    paths = config.paths()
    store = OS.store_from_env()
    result: dict[str, Any] = {"profile": config.profile()["name"],
                              "dataset": OS.save_dataset(paths.data_dir, store)}

    # Every report version on disk that the store does not already hold. An edition is immutable, so
    # this is an upload of what is new rather than a synchronisation of what has changed.
    published = []
    out_dir = paths.output_dir
    if out_dir.exists():
        for type_dir in sorted(p for p in out_dir.iterdir() if p.is_dir() and p.name != "latest"):
            for edition_dir in sorted(p for p in type_dir.iterdir() if p.is_dir()):
                for version_dir in sorted(p for p in edition_dir.iterdir() if p.is_dir()):
                    try:
                        version = int(version_dir.name.split("_")[0].lstrip("v"))
                    except (ValueError, IndexError):
                        continue
                    res = OS.publish_edition(version_dir, type_dir.name, edition_dir.name, version, store)
                    if res["uploaded"]:
                        published.append({"report_type": type_dir.name, "edition": edition_dir.name,
                                          "version": version, "files": len(res["uploaded"])})
    result["editions_published"] = published
    return _out(result)


def _archive_editions() -> list[dict[str, Any]]:
    """The archive as the dashboard should list it, read from what is actually on disk.

    Built from the local archive index rather than from the store listing, because the manifest is
    what carries the fingerprint, the quality summary and the validated findings, and the store only
    knows about bytes. It therefore needs no object store, which is why rebuilding the projection
    works against a dataset alone.
    """
    from ..scheduling import archive

    index = archive.index()
    out: list[dict[str, Any]] = []
    for report_type, editions in (index.get("report_types") or {}).items():
        for e in editions:
            version_dir = Path(e["path"])
            manifest_path = version_dir / "manifest.json"
            manifest = {}
            if manifest_path.exists():
                try:
                    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                except ValueError:
                    manifest = {}
            summary = _summary_lines(version_dir, manifest)
            files = [{"name": f.name, "bytes": f.stat().st_size,
                      "key": f"reports/{report_type}/{e['edition']}/v{e['latest_version']}/{f.name}"}
                     for f in sorted(version_dir.iterdir())
                     if f.is_file() and f.suffix in (".pdf", ".pptx", ".xlsx")]
            out.append({
                "report_type": report_type, "edition": e["edition"], "version": e["latest_version"],
                "generated_at": manifest.get("generated_at"), "as_of": manifest.get("as_of"),
                "status_label": manifest.get("status_label"),
                "partial": bool(manifest.get("partial_edition")),
                "n_slides": manifest.get("n_slides"),
                "fingerprint": (manifest.get("edition_fingerprint") or {}).get("fingerprint"),
                "trigger": (manifest.get("edition_trigger") or {}).get("trigger"),
                "narrative_mode": manifest.get("narrative_mode"),
                "numbers_checked": (manifest.get("narrative_validation") or {}).get("numbers_checked"),
                "quality": manifest.get("quality_summary") or {},
                "reporting_periods": manifest.get("reporting_periods") or {},
                "summary": summary, "files": files,
                "blob_prefix": f"reports/{report_type}/{e['edition']}/v{e['latest_version']}",
                "is_latest": True,
            })
    return out


def _summary_lines(version_dir: Path, manifest: dict[str, Any]) -> list[str]:
    """The edition's own validated findings, reused rather than rewritten.

    The same function the delivery layer uses, so the summary on the dashboard is the summary in the
    email is the summary on the deck. A finding the validator rejected appears in none of them.
    """
    narrative_path = version_dir / "narrative.json"
    if not narrative_path.exists():
        return []
    try:
        narrative = json.loads(narrative_path.read_text(encoding="utf-8"))
    except ValueError:
        return []
    from ..delivery.compose import _findings

    return _findings(manifest, narrative, limit=5)


def cmd_readmodel(args) -> int:
    from ..calc.validate import validate_all
    from ..storage.db import Database
    from . import readmodel as RM

    paths = config.paths()
    conn = RM.connect()
    db = Database(paths.db_path)
    try:
        RM.ensure_schema(conn)
        result = {
            "indicators": RM.publish_indicators(conn, db),
            "publications": RM.publish_publications(conn, db),
            "editions": RM.publish_editions(conn, _archive_editions()),
            "deliveries": RM.publish_deliveries(conn, paths.data_dir / "deliveries.sqlite"),
            "definitions": RM.publish_definitions(conn),
        }
        quality = validate_all(db, write=False)
        result["quality_checks"] = RM.publish_quality(conn, quality)
        RM.set_meta(conn, "last_readmodel_publish", {
            "at": __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat(timespec="seconds"),
            "counts": result, "quality": quality["summary"],
        })
        return _out(result)
    finally:
        db.close()
        conn.close()


def cmd_record_run(args) -> int:
    """Record one scheduled run in the read model, so the dashboard can report on it."""
    from . import readmodel as RM

    paths = config.paths()
    summary_path = Path(args.summary) if args.summary else (
        paths.state_dir / f"last_{args.task.replace('-', '_')}.json")
    if not summary_path.exists():
        return _out({"error": f"no run summary at {summary_path}"}, 2)
    summary = json.loads(summary_path.read_text(encoding="utf-8"))

    conn = RM.connect()
    try:
        RM.ensure_schema(conn)
        RM.record_run(conn, {
            "run_id": args.run_id or os.environ.get("GITHUB_RUN_ID") or summary.get("started_at"),
            "task": args.task, "trigger": args.trigger, "status": summary.get("status", "unknown"),
            "started_at": summary.get("started_at"), "finished_at": summary.get("finished_at"),
            "local_time": summary.get("local_time"),
            "produced": summary.get("produced") or [],
            "delivered": summary.get("delivered_count") or 0,
            "readiness": (summary.get("steps") or {}).get("readiness") or {},
            "error": summary.get("error"),
            "detail": {k: v for k, v in (summary.get("steps") or {}).items()
                       if k in ("refresh", "validate", "stale_sources", "monitor", "window")},
        })
        return _out({"recorded": args.task, "status": summary.get("status")})
    finally:
        conn.close()


def cmd_status(args) -> int:
    paths = config.paths()
    result: dict[str, Any] = {"data_dir": str(paths.data_dir), "on_disk": OS.dataset_summary(paths.data_dir)}
    try:
        store = OS.store_from_env()
        pointer = store.get(OS.POINTER_KEY)
        result["store"] = {"configured": type(store).__name__,
                           "dataset": json.loads(pointer) if pointer else None,
                           "report_objects": len(store.list("reports/"))}
    except OS.StorageError as exc:
        result["store"] = {"error": str(exc)}
    try:
        from . import readmodel as RM

        conn = RM.connect()
        try:
            RM.ensure_schema(conn)
            with conn.cursor() as cur:
                counts = {}
                for table in ("indicators", "publications", "editions", "job_runs", "deliveries",
                              "quality_checks"):
                    counts[table] = cur.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            result["read_model"] = {"counts": counts, "last_publish": RM.get_meta(conn, "last_readmodel_publish")}
        finally:
            conn.close()
    except Exception as exc:
        result["read_model"] = {"error": f"{type(exc).__name__}: {exc}"}
    return _out(result)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="azmonitor.cloud.publish", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="command", required=True)
    sub.add_parser("restore", help="pull the dataset out of object storage").set_defaults(fn=cmd_restore)
    sub.add_parser("save", help="push the dataset and any new report editions up").set_defaults(fn=cmd_save)
    sub.add_parser("readmodel", help="rebuild the projection the dashboard reads").set_defaults(fn=cmd_readmodel)
    s = sub.add_parser("record-run", help="record one scheduled run in the read model")
    s.add_argument("--task", required=True)
    s.add_argument("--trigger", default="github")
    s.add_argument("--run-id")
    s.add_argument("--summary", help="path to the run summary JSON (default: the task's own state file)")
    s.set_defaults(fn=cmd_record_run)
    sub.add_parser("status", help="what the store and the read model hold").set_defaults(fn=cmd_status)

    args = ap.parse_args(argv)
    setup_logging(config.paths().logs_dir)
    try:
        return args.fn(args)
    except Exception as exc:
        log.exception("%s failed", args.command)
        return _out({"command": args.command, "error": f"{type(exc).__name__}: {exc}",
                     "traceback": traceback.format_exc().splitlines()[-6:]}, 1)


if __name__ == "__main__":
    sys.exit(main())
