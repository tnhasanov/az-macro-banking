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
import datetime as dt
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


def _fence():
    """The lease this worker holds, for checking ownership before anything persistent.

    The workflow takes the lease in its own step and passes the holder and fence token down through
    the environment, so every later command can prove it is still the worker that started the run.
    Returns None when no database is configured — a single-host deployment has a file lock and does
    not need this.
    """
    from . import lock as L, readmodel as RM

    holder = os.environ.get("AZMONITOR_LEASE_HOLDER")
    fence = os.environ.get("AZMONITOR_LEASE_FENCE")
    if not holder or not RM.dsn_or_none():
        return None
    conn = RM.connect()
    lease = L.DatabaseLease(conn, os.environ.get("AZMONITOR_LEASE_NAME", "azmonitor-run"),
                            holder=holder, fence=int(fence) if fence else None)
    return lease


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


class RestrictedArtefact(RuntimeError):
    """A file about to be uploaded carries the organisation's identity.

    Raised rather than logged, because the alternative is a partial upload: some editions in the
    store and the rest refused, with no record of which. Stopping the whole publish keeps the
    archive in a state someone can reason about.
    """

    def __init__(self, report_type: str, edition: str, version: int, findings: dict):
        self.report_type, self.edition, self.version = report_type, edition, version
        self.findings = findings
        listed = "; ".join(f"{Path(f).name}: {', '.join(w)}" for f, w in list(findings.items())[:3])
        super().__init__(
            f"{report_type} {edition} v{version} would upload material that identifies the "
            f"organisation ({listed}). These files were rendered under a branded profile; the "
            f"neutral profile only affects what is rendered from now on. Re-render them with "
            f"AZMONITOR_PROFILE=neutral, or set {UPLOAD_OVERRIDE}={UPLOAD_OVERRIDE_VALUE} if you "
            f"own this content and intend to publish it.")


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
    fence = _fence()
    result: dict[str, Any] = {"profile": config.profile()["name"],
                              "dataset": OS.save_dataset(paths.data_dir, store, fence=fence)}

    allow = os.environ.get(UPLOAD_OVERRIDE) == UPLOAD_OVERRIDE_VALUE
    try:
        uploaded = _publish_local_editions(paths.output_dir, store, fence=fence,
                                           allow_restricted=allow)
    except RestrictedArtefact as exc:
        log.error("%s", exc)
        return _out({**result, "error": str(exc), "restricted": exc.findings,
                     "editions_published": []}, 3)
    result["editions_published"] = uploaded["published"]
    result["catalogue_entries"] = len(store.list(OS.CATALOG_PREFIX))
    return _out(result)


def _publish_local_editions(out_dir: Path, store, *, fence,
                            allow_restricted: bool = False) -> dict[str, Any]:
    """Upload every report version on disk that the store does not already hold.

    An edition is immutable, so this is an upload of what is new rather than a synchronisation of
    what has changed. The catalogue entry for each version goes up after its files, so an entry
    never describes an edition whose files are not there yet.

    Every artefact is opened and read before it is uploaded. The distribution profile governs how
    the engine renders; it says nothing about files rendered earlier under a different one, and an
    archive of those is exactly what a first seed uploads. Checking the profile alone let 95 branded
    decks, 36 branded workbooks and 95 branded PDFs through under the neutral profile.
    """
    from . import artifacts

    published: list[dict[str, Any]] = []
    if not out_dir.exists():
        return {"published": published, "note": "no output directory on this worker"}

    for type_dir in sorted(p for p in out_dir.iterdir() if p.is_dir() and p.name != "latest"):
        for edition_dir in sorted(p for p in type_dir.iterdir() if p.is_dir()):
            # A version number can appear on disk more than once — `v1_<stamp>` twice, from two
            # renders of the same version during development. Only one of them can be version 1 in
            # the store, because a published version is immutable. Taking the newest directory is
            # the deterministic choice; uploading both put two renders under one key and left the
            # loser's files with no catalogue entry, which is what `publish verify` reported.
            newest: dict[int, Path] = {}
            for version_dir in sorted(p for p in edition_dir.iterdir() if p.is_dir()):
                try:
                    version = int(version_dir.name.split("_")[0].lstrip("v"))
                except (ValueError, IndexError):
                    continue
                previous = newest.get(version)
                if previous is None or version_dir.name > previous.name:
                    newest[version] = version_dir

            for version, version_dir in sorted(newest.items()):
                if fence:
                    fence.check(f"before publishing {type_dir.name} {edition_dir.name} v{version}")

                if not allow_restricted:
                    uploadable = [f for f in sorted(version_dir.iterdir())
                                  if f.is_file() and not f.name.startswith(".")]
                    findings = artifacts.screen(uploadable)
                    if findings:
                        raise RestrictedArtefact(type_dir.name, edition_dir.name, version, findings)

                res = OS.publish_edition(version_dir, type_dir.name, edition_dir.name, version, store)
                OS.write_catalog_entry(
                    _catalog_entry(version_dir, type_dir.name, edition_dir.name, version), store)
                if res["uploaded"]:
                    published.append({"report_type": type_dir.name, "edition": edition_dir.name,
                                      "version": version, "files": len(res["uploaded"])})
    return {"published": published, "editions_uploaded": len(published)}


def _catalog_entry(version_dir: Path, report_type: str, edition: str, version: int) -> dict[str, Any]:
    """Everything the dashboard needs about one edition version, read from its own manifest.

    Written once, at publication, and never rewritten. It carries the fingerprint that decided the
    edition was worth producing, the reporting period of every input, the quality result at the
    time, and the findings the narrative validator passed — so the record of what was published
    survives the runner that published it.
    """
    manifest: dict[str, Any] = {}
    manifest_path = version_dir / "manifest.json"
    if manifest_path.exists():
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except ValueError:
            manifest = {}
    files = [{"name": f.name, "bytes": f.stat().st_size,
              "key": f"reports/{report_type}/{edition}/v{version}/{f.name}"}
             for f in sorted(version_dir.iterdir())
             if f.is_file() and f.suffix in (".pdf", ".pptx", ".xlsx")]
    return {
        "report_type": report_type, "edition": edition, "version": version,
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
        "summary": _summary_lines(version_dir, manifest),
        "files": files,
        "blob_prefix": f"reports/{report_type}/{edition}/v{version}",
    }


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
    """Rebuild the projection the dashboard reads.

    The report catalogue comes from object storage, not from this runner's disk. That is the whole
    point: a fresh runner has no `outputs/` directory, and building the catalogue from what it can
    see locally would publish an empty archive over a full one.
    """
    from . import readmodel as RM

    store = OS.store_from_env()
    fence = _fence()
    if fence:
        fence.check("before publishing the read model")
    conn = RM.connect()
    try:
        return _out(refresh_readmodel(conn, store))
    finally:
        conn.close()


def refresh_readmodel(conn, store) -> dict[str, Any]:
    """Rebuild the dashboard's projection from the dataset on disk and the catalogue in storage.

    Called by the job worker while it still holds the dataset lease, so a worker with older data
    can never overwrite the projection a newer one has just published.
    """
    from ..calc.validate import validate_all
    from ..storage.db import Database
    from . import readmodel as RM

    paths = config.paths()
    db = Database(paths.db_path)
    try:
        RM.ensure_schema(conn)
        catalogue = OS.read_catalog(store)
        broken = [f"{e['report_type']}/{e['edition']}/v{e['version']}"
                  for e in catalogue if e.get("files_missing")]
        result = {
            "indicators": RM.publish_indicators(conn, db),
            "publications": RM.publish_publications(conn, db),
            "editions": RM.publish_editions(conn, catalogue),
            "deliveries": RM.publish_deliveries(conn, paths.data_dir / "deliveries.sqlite"),
            "definitions": RM.publish_definitions(conn),
        }
        if broken:
            # Reported, not hidden: an edition whose files have gone is a storage problem, and
            # dropping it from the catalogue would make it look like a report nobody ever produced.
            result["editions_with_missing_files"] = broken
            log.warning("%d catalogued edition(s) reference files the store does not hold: %s",
                        len(broken), ", ".join(broken[:5]))
        quality = validate_all(db, write=False)
        result["quality_checks"] = RM.publish_quality(conn, quality)
        RM.set_meta(conn, "last_readmodel_publish", {
            "at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
            "counts": result, "quality": quality["summary"],
        })
        return result
    finally:
        db.close()


def cmd_verify(args) -> int:
    """Check that the catalogue and the store agree, without changing either.

    Two failures worth catching before anyone relies on a download link: an edition the catalogue
    lists whose files are not in the store, and files in the store that no catalogue entry claims.
    The first breaks a download; the second is an upload that died before its entry was written and
    is recoverable by re-running `save`.
    """
    store = OS.store_from_env()
    catalogue = OS.read_catalog(store)
    catalogued_keys = {f["key"] for e in catalogue for f in (e.get("files") or [])}
    stored = {b["key"] for b in store.list("reports/")
              if b["key"].rsplit(".", 1)[-1] in ("pdf", "pptx", "xlsx")}

    broken = [{"edition": f"{e['report_type']}/{e['edition']}/v{e['version']}",
               "missing": e["files_missing"]}
              for e in catalogue if e.get("files_missing")]
    orphans = sorted(stored - catalogued_keys)

    result = {
        "catalogue_entries": len(catalogue),
        "files_catalogued": len(catalogued_keys),
        "files_in_store": len(stored),
        "editions_with_missing_files": broken,
        "files_with_no_catalogue_entry": orphans[:20],
        "orphan_count": len(orphans),
        "ok": not broken,
    }
    if orphans:
        result["note"] = ("files with no catalogue entry are usually an upload that was "
                          "interrupted before its entry was written; re-running `save` writes it")
    return _out(result, 0 if not broken else 5)


def cmd_record_run(args) -> int:
    """Record one scheduled run in the read model, so the dashboard can report on it.

    Recorded for a failed run too, and `--published` says whether the read model was refreshed from
    it. A failed cycle that left no trace would look on the dashboard exactly like a cycle that
    never happened, which is the difference between "nothing to report" and "the engine is down".
    """
    from . import readmodel as RM

    paths = config.paths()
    summary_path = Path(args.summary) if args.summary else (
        paths.state_dir / f"last_{args.task.replace('-', '_')}.json")
    if summary_path.exists():
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
    elif args.status:
        # A run that died before writing its own summary still gets a row, built from what the
        # workflow knows. Without this the most serious failures are the ones that vanish.
        summary = {"status": args.status, "error": args.error,
                   "started_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")}
    else:
        return _out({"error": f"no run summary at {summary_path}"}, 2)

    conn = RM.connect()
    try:
        RM.ensure_schema(conn)
        RM.record_run(conn, {
            "run_id": args.run_id or os.environ.get("GITHUB_RUN_ID") or summary.get("started_at"),
            "task": args.task, "trigger": args.trigger,
            "status": args.status or summary.get("status", "unknown"),
            "published": bool(args.published),
            "started_at": summary.get("started_at"), "finished_at": summary.get("finished_at"),
            "local_time": summary.get("local_time"),
            "produced": summary.get("produced") or [],
            "delivered": summary.get("delivered_count") or 0,
            "readiness": (summary.get("steps") or {}).get("readiness") or {},
            "error": args.error or summary.get("error"),
            "detail": {k: v for k, v in (summary.get("steps") or {}).items()
                       if k in ("refresh", "validate", "stale_sources", "monitor", "window")},
        })
        return _out({"recorded": args.task, "status": args.status or summary.get("status"),
                     "published": bool(args.published)})
    finally:
        conn.close()


def cmd_seed(args) -> int:
    """Put the existing validated dataset into an empty store, once.

    The dataset is not in git — it is 374 MB of SQLite and downloaded source documents — so the
    first cloud run has to get it from the machine that already holds it. That transfer is the one
    moment when an empty store is expected, which makes it the one moment when a mistake is
    indistinguishable from normal operation unless it is checked.

    So this refuses to do anything to a store that already holds a dataset, checks the local
    dataset is complete before sending it, and verifies the digest afterwards by reading back what
    the store now has. `--check` does everything except the upload.
    """
    paths = config.paths()
    store = OS.store_from_env()
    result: dict[str, Any] = {"data_dir": str(paths.data_dir), "profile": config.profile()["name"]}

    refusal = _refuse_restricted_upload()
    if refusal:
        return _out({**result, **refusal}, 3)

    # --- is there anything here to overwrite?
    pointer = store.get(OS.POINTER_KEY)
    existing = json.loads(pointer) if pointer else None
    result["store_already_holds"] = existing
    if existing and not args.replace:
        return _out({**result, "seeded": False,
                     "error": "this store already holds a dataset saved at "
                              f"{existing.get('saved_at')}; seeding would replace it",
                     "remedy": "run `publish restore` to check it is the one you expect, or pass "
                               "--replace if you are certain you mean to overwrite it"}, 4)

    # --- is the local dataset complete?
    missing = [part for part in OS.DATASET_PARTS if not (paths.data_dir / part).exists()]
    summary = OS.dataset_summary(paths.data_dir)
    result["contents"] = summary
    result["missing_parts"] = missing
    if "monitor.sqlite" in missing:
        return _out({**result, "seeded": False,
                     "error": "there is no monitor.sqlite in the data directory; this is not a "
                              "dataset and must not be uploaded as one"}, 2)

    # --- would it carry anything it should not?
    strays = _confidential_strays(paths.data_dir)
    result["unexpected_files"] = strays
    if strays and not args.allow_unexpected:
        return _out({**result, "seeded": False,
                     "error": f"{len(strays)} file(s) in the data directory are not part of the "
                              "dataset and would be uploaded with it",
                     "remedy": "remove them, or pass --allow-unexpected if they belong there"}, 2)

    if args.check:
        return _out({**result, "seeded": False, "checked_only": True,
                     "would_upload_parts": [p for p in OS.DATASET_PARTS if p not in missing]})

    # --- send it, then read back what arrived
    saved = OS.save_dataset(paths.data_dir, store)
    result["uploaded"] = saved

    # Read back what the store now holds, rather than trusting the upload call. The dataset is two
    # objects, and both have to be there: a dataset missing either half is not a smaller dataset.
    verified = []
    for name in ("mutable", "static"):
        piece = saved[name]
        stored = store.stat(piece["key"])
        verified.append({"part": name, "key": piece["key"], "bytes_expected": piece["bytes"],
                         "bytes_in_store": (stored or {}).get("size"), "sha256": piece["sha256"]})
        if not stored:
            return _out({**result, "seeded": False, "verified": verified,
                         "error": f"{piece['key']} was uploaded but the store does not list it"}, 1)
        if stored.get("size") not in (None, piece["bytes"]):
            return _out({**result, "seeded": False, "verified": verified,
                         "error": f"the store holds {stored['size']} bytes of {piece['key']} where "
                                  f"{piece['bytes']} were sent; the upload did not complete"}, 1)
    result["verified"] = verified

    result["seeded"] = True

    # The dataset carries the figures; the report archive is separate objects and its own
    # catalogue. Seeding only the dataset leaves a dashboard with every indicator and no reports,
    # which is a confusing first impression of a working system.
    if args.with_reports:
        allow = os.environ.get(UPLOAD_OVERRIDE) == UPLOAD_OVERRIDE_VALUE
        try:
            result["reports"] = _publish_local_editions(paths.output_dir, store, fence=None,
                                                        allow_restricted=allow)
        except RestrictedArtefact as exc:
            log.error("%s", exc)
            return _out({**result, "seeded": True, "reports_uploaded": False,
                         "error": str(exc), "restricted": exc.findings,
                         "note": "the dataset is in the store; the report archive is not"}, 3)

    result["next"] = ["python -m azmonitor.cloud.publish restore   # on a fresh worker",
                      "python -m azmonitor.cloud.publish readmodel",
                      "python -m azmonitor.cloud.publish verify"]
    if not args.with_reports:
        result["next"].insert(0, "python -m azmonitor.cloud.publish save   # to upload the report archive")
    return _out(result)


def _confidential_strays(data_dir: Path) -> list[str]:
    """Files in the data directory that are not part of the dataset.

    The dataset is four named things. Anything else sitting in the directory would be swept into
    the tarball and sent to personal cloud storage, which is the mistake worth catching before the
    upload rather than after.
    """
    if not data_dir.exists():
        return []
    expected = set(OS.DATASET_PARTS)
    strays = []
    for child in sorted(data_dir.iterdir()):
        if child.name in expected or child.name in ("logs", "backups", "analytics", "snapshots"):
            continue
        strays.append(child.name)
    return strays


def cmd_check(args) -> int:
    """Does the configured credential open the store? Nothing is written either way.

    Separate from `status`, which reports what the store holds and tolerates a store it cannot
    reach. This one exists to be a gate: it exits non-zero when the credential does not work, so a
    workflow can stop before a run that would spend an hour and then fail to save.
    """
    try:
        store = OS.store_from_env()
        result = store.check()
    except OS.StorageError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, indent=2))
        return 1
    result["ok"] = bool(result.get("reachable"))
    print(json.dumps(result, indent=2))
    return 0 if result["ok"] else 1


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
    s.add_argument("--status", help="override the status, for a run that died before writing one")
    s.add_argument("--error", help="what went wrong, when there is no summary to read it from")
    s.add_argument("--published", action="store_true",
                   help="the read model was refreshed from this run")
    s.set_defaults(fn=cmd_record_run)
    sub.add_parser("verify", help="check that the catalogue and the store agree").set_defaults(fn=cmd_verify)
    s = sub.add_parser("seed", help="put the existing validated dataset into an empty store")
    s.add_argument("--check", action="store_true", help="verify everything, upload nothing")
    s.add_argument("--replace", action="store_true",
                   help="overwrite a dataset the store already holds (say why in the run log)")
    s.add_argument("--allow-unexpected", action="store_true",
                   help="upload even though the data directory holds files that are not the dataset")
    s.add_argument("--with-reports", action="store_true",
                   help="also upload the report archive and its catalogue, so the dashboard has "
                        "history from the first run")
    s.set_defaults(fn=cmd_seed)
    sub.add_parser("status", help="what the store and the read model hold").set_defaults(fn=cmd_status)
    sub.add_parser(
        "check", help="prove the Blob credential opens the store (writes nothing)",
    ).set_defaults(fn=cmd_check)

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
