"""Report edition orchestration: fact pack -> narrative -> deck -> PDF/previews -> workbook -> manifest -> latest pointer."""
from __future__ import annotations

import datetime as dt
import json
import shutil
from pathlib import Path
from typing import Any

from . import config
from .facts import FactPackBuilder
from .narrative import api as narrative_api
from .narrative import facts_only
from .narrative.contract import flatten
from .scheduling.fingerprint import describe_change, fingerprint
from .narrative.validate import apply_fallback, validate_narrative
from .render.monthly import render_monthly
from .render.pdf import convert_to_pdf, previews, soffice_available
from .render.workbook import build_workbook
from .storage.db import Database, utcnow
from .storage.snapshot import export_snapshot
from .util.log import get_logger, setup_logging
from .util.periods import parse_as_of

log = get_logger("reports")


def _edition_dir(paths, report_type: str, edition: str, version: int, ts: str) -> Path:
    return paths.output_dir / report_type / edition / f"v{version}_{ts}"


def _next_version(db: Database, report_type: str, edition: str) -> int:
    eds = [e for e in db.editions(report_type) if e["edition_period"] == edition]
    return (max((e["version"] for e in eds), default=0) + 1)


def _update_latest(paths, report_type: str, edition_dir: Path, manifest: dict[str, Any]) -> None:
    latest = paths.output_dir / "latest"
    latest.mkdir(parents=True, exist_ok=True)
    target = latest / report_type
    if target.is_symlink() or target.exists():
        if target.is_symlink():
            target.unlink()
        else:
            shutil.rmtree(target)
    try:
        target.symlink_to(edition_dir.resolve(), target_is_directory=True)
    except OSError:
        shutil.copytree(edition_dir, target)
    (latest / f"{report_type}.json").write_text(json.dumps({"edition_dir": str(edition_dir), "manifest": manifest["manifest_path"], "generated_at": manifest["generated_at"],
                                                              "edition": manifest["edition"], "version": manifest["version"]}, indent=2), encoding="utf-8")


def evidence_passages(db: Database, fp: dict[str, Any]) -> dict[str, Any]:
    """Passages a narrative may quote: everything extracted from the publications this edition cites."""
    out: dict[str, Any] = {}
    pub_ids = [p["publication_id"] for p in (fp.get("publications") or {}).get("all", [])] or None
    rows = db.conn.execute("SELECT * FROM passages").fetchall() if pub_ids is None else []
    if pub_ids is not None:
        marks = ",".join("?" for _ in pub_ids)
        rows = db.conn.execute(f"SELECT * FROM passages WHERE publication_id IN ({marks})", pub_ids).fetchall()
    for r in rows:
        out[r["passage_id"]] = {"text": r["text"], "doc_id": r["doc_id"], "publication_id": r["publication_id"],
                                "language": r["language"], "page_index": r["page_index"], "printed_page": r["printed_page"],
                                "cite": f"page {r['page_index']}" + (f" (printed {r['printed_page']})" if r["printed_page"] else "")}
    return out


def resolve_narrative(fp: dict[str, Any], facts_only_flag: bool, narrative_file: str | None, lang: str,
                      passages: dict[str, Any] | None = None) -> tuple[dict[str, Any], dict[str, Any]]:
    fallback = facts_only.generate(fp)
    settings = config.settings().get("narrative", {})
    usage: dict[str, Any] = {}
    if narrative_file:
        nar = json.loads(Path(narrative_file).read_text(encoding="utf-8"))
        nar.setdefault("mode", "analyst_file")
    elif not facts_only_flag and settings.get("provider") == "api":
        ok, why = narrative_api.available()
        if ok:
            nar, usage = narrative_api.generate(fp, lang)
        else:
            log.warning("API narrative requested but unavailable (%s); using facts-only", why)
            nar = fallback
    else:
        nar = fallback
    if nar is not fallback:
        # Slides the author did not write are filled from the facts-only generator and labelled, so a
        # focused commentary does not leave the rest of the deck without text.
        merged = dict(nar)
        merged["slides"] = dict(fallback.get("slides") or {})
        for sid, block in (nar.get("slides") or {}).items():
            merged["slides"][sid] = block
        for sid in merged["slides"]:
            if sid not in (nar.get("slides") or {}):
                merged["slides"][sid] = dict(merged["slides"][sid] or {})
                merged["slides"][sid]["source"] = "facts_only"
        nar = merged
    validation = validate_narrative(nar, fp, passages or {})
    if nar is not fallback:
        nar = apply_fallback(nar, fallback, validation)
    else:
        nar["validation"] = validation
    # facts-only text is generated from the fact pack itself; a failure here would be a formatting bug and is reported
    if nar is fallback and validation.get("problems"):
        log.warning("facts-only narrative validation reported %d problems", len(validation["problems"]))
    nar["usage"] = usage
    return nar, validation


def blocking_quality_failures(fp: dict[str, Any], settings: dict[str, Any]) -> list[dict[str, Any]]:
    """Checks that must stop a new edition.

    A check is blocking when it failed on a period this edition displays and no explicit exception in
    `config/quality_exceptions.yaml` covers it. The override in settings exists so an operator can
    publish deliberately in an emergency; it is off by default and is recorded in the manifest.
    """
    if (settings.get("quality") or {}).get("allow_publication_with_critical_failures", False):
        return []
    return [c for c in (fp.get("quality") or {}).get("checks", [])
            if not c.get("ok") and c.get("severity") == "critical"]


def generate_monthly(as_of: str | None, facts_only_flag: bool, narrative_file: str | None, lang: str,
                     force: bool = False, db: Database | None = None, dry_run: bool = False) -> dict[str, Any]:
    """Produce the monthly edition, or say what producing it would do.

    `dry_run` goes as far as the decision and stops: it builds the fact pack, compares the
    fingerprint and reports `would_generate`, `unchanged` or `blocked` without rendering anything.
    A dry run that reported "would generate" where a real run says "unchanged" would be worse than
    no dry run at all, because it would be believed.
    """
    paths = config.paths()
    paths.ensure()
    setup_logging(paths.logs_dir)
    own = db is None
    db = db or Database(paths.db_path)
    as_of_d = parse_as_of(as_of)
    builder = FactPackBuilder(db, as_of_d, lang=lang)
    fp = builder.build()
    fp["report_type"] = "monthly"
    anchors = fp["anchors"]
    blocked = [k for k, v in anchors.items() if not v.get("verified") or not v.get("period_end")]
    if blocked:
        status = {"status": "blocked", "cause": f"critical anchors missing or unverified: {blocked}", "as_of": as_of_d.isoformat(), "at": utcnow(), "anchors": anchors}
        (paths.state_dir / "monthly_status.json").write_text(json.dumps(status, indent=2, default=str), encoding="utf-8")
        log.error("monthly report blocked: %s", status["cause"])
        if own:
            db.close()
        return status
    edition = fp["edition"]["edition_month"]
    mode = "analyst_file" if narrative_file else ("facts_only" if facts_only_flag else config.settings().get("narrative", {}).get("provider", "none"))
    current_fp = fingerprint(fp, db, narrative_file, mode)
    # An edition is due whenever any input the report speaks about has changed: a revision to a
    # displayed value, a new publication or decision, a different narrative, or new configuration.
    # The banking month on its own decides nothing.
    prior_any = [e for e in db.editions("monthly") if e["status"] == "generated"]
    prior = [e for e in prior_any if e["edition_period"] == edition]
    last_manifest = None
    if prior_any:
        try:
            last_manifest = json.loads(Path(prior_any[-1]["manifest_path"]).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            last_manifest = None
    change = describe_change((last_manifest or {}).get("edition_fingerprint"), current_fp)
    if prior and not force and last_manifest and \
            (last_manifest.get("edition_fingerprint") or {}).get("fingerprint") == current_fp["fingerprint"]:
        res = {"status": "unchanged", "edition": edition, "existing": prior[-1]["path"],
               "fact_pack_hash": fp.get("fact_pack_hash"), "fingerprint": current_fp["fingerprint"],
               "note": "every input of the last edition is unchanged; use --force to regenerate"}
        if own:
            db.close()
        return res
    # a critical data-quality failure blocks publication of a new edition
    blocking = blocking_quality_failures(fp, config.settings())
    if dry_run and not blocking:
        res = {"status": "would_generate", "edition": edition, "fact_pack_hash": fp.get("fact_pack_hash"),
               "fingerprint": current_fp["fingerprint"], "trigger": change,
               "note": "the inputs differ from the last edition; a real run would produce a new version"}
        if own:
            db.close()
        return res
    if blocking:
        status = {"status": "blocked", "cause": "critical data-quality checks failed", "as_of": as_of_d.isoformat(),
                  "at": utcnow(), "failed_checks": [{k: c.get(k) for k in ("id", "type", "series_id", "period_end", "diff", "message")}
                                                    for c in blocking[:20]],
                  "note": "the last successful edition remains current; fix the source data or record an explicit, "
                          "justified exception in config/quality_exceptions.yaml"}
        if not dry_run:
            (paths.state_dir / "monthly_status.json").write_text(json.dumps(status, indent=2, default=str), encoding="utf-8")
        log.error("monthly report blocked by %d critical quality failure(s)", len(blocking))
        if own:
            db.close()
        return status
    version = _next_version(db, "monthly", edition)
    ts = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out = _edition_dir(paths, "monthly", edition, version, ts)
    out.mkdir(parents=True, exist_ok=True)
    stem = f"AZ_Macro_Banking_Monitor_{edition}_v{version}_{ts}"
    (out / "fact_pack.json").write_text(json.dumps(fp, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    nar, validation = resolve_narrative(fp, facts_only_flag, narrative_file, lang, evidence_passages(db, fp))
    (out / "narrative.json").write_text(json.dumps(nar, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    partial = bool(fp["availability"]["missing"])
    # snapshot
    snap = export_snapshot(db, paths.snapshots_dir, paths.analytics_dir, builder.eng.table())
    # deck and workbook render the flattened text; the grounded form stays in narrative.json
    rendered = flatten(nar)
    pptx_path = out / f"{stem}.pptx"
    deck_info = render_monthly(fp, rendered, pptx_path, lang)
    xlsx_path = build_workbook(fp, rendered, db, out / f"{stem}.xlsx", builder.eng.table())
    # pdf + previews
    pdf_info: dict[str, Any] = {"status": "skipped", "reason": "LibreOffice not available"}
    preview_files: list[str] = []
    if soffice_available():
        try:
            pdf = convert_to_pdf(pptx_path, out)
            pdf_info = {"status": "ok", "path": str(pdf)}
            try:
                preview_files = [str(p) for p in previews(pdf, out / "previews")]
            except Exception as exc:  # previews are inspection aids only
                pdf_info["previews_error"] = str(exc)
        except Exception as exc:
            pdf_info = {"status": "failed", "reason": str(exc)}
    quality_path = out / "quality_report.json"
    quality_path.write_text(json.dumps(fp["quality"], ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    manifest = {
        "report_type": "monthly", "edition": edition, "version": version, "generated_at": utcnow(), "as_of": as_of_d.isoformat(), "as_of_definition": fp["as_of_definition"],
        "information_set_mode": fp["information_set_mode"], "status_label": config.settings()["report"]["status_label"], "partial_edition": partial,
        "missing_inputs": fp["availability"]["missing"], "reporting_periods": fp["edition"], "anchors": anchors, "fact_pack_hash": fp["fact_pack_hash"],
        "narrative_mode": nar.get("mode"), "narrative_validation": {"numbers_checked": validation.get("numbers_checked"), "problems": validation.get("problems"), "rejected_slides": validation.get("rejected_slides"),
                                                                     "rejected_findings": validation.get("rejected_findings")},
        "api_usage": nar.get("usage") or {}, "snapshot": snap, "files": {"pptx": str(pptx_path), "xlsx": str(xlsx_path), "pdf": pdf_info, "previews": preview_files,
                                                                        "fact_pack": str(out / "fact_pack.json"), "narrative": str(out / "narrative.json"), "quality": str(quality_path)},
        "slides": deck_info["slides"], "n_slides": deck_info["n_slides"], "quality_summary": fp["quality"]["summary"], "language": lang,
        "edition_fingerprint": current_fp, "edition_trigger": change,
        "config_versions": {k: _file_hash(config.CONFIG_DIR / f"{k}.yaml") for k in ("settings", "sources", "metrics", "reports", "theme", "glossary")},
    }
    manifest["manifest_path"] = str(out / "manifest.json")
    (out / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    db.add_edition({"edition_id": f"monthly:{edition}:v{version}", "report_type": "monthly", "edition_period": edition, "version": version, "generated_at": manifest["generated_at"],
                    "as_of": as_of_d.isoformat(), "snapshot_id": snap["snapshot_id"], "status": "generated", "path": str(out), "manifest_path": manifest["manifest_path"],
                    "anchors": json.dumps(fp["edition"]), "fingerprint": current_fp["fingerprint"], "trigger": change["trigger"],
                    "narrative_mode": mode})
    _update_latest(paths, "monthly", out, manifest)
    (paths.state_dir / "monthly_status.json").write_text(json.dumps({"status": "generated", "edition": edition, "version": version, "path": str(out), "at": manifest["generated_at"],
                                                                     "partial": partial}, indent=2), encoding="utf-8")
    if own:
        db.close()
    return {"status": "generated", "edition": edition, "version": version, "path": str(out), "pptx": str(pptx_path), "pdf": pdf_info, "xlsx": str(xlsx_path), "n_slides": deck_info["n_slides"],
            "partial": partial, "missing_inputs": fp["availability"]["missing"], "narrative_mode": nar.get("mode"), "narrative_problems": len(validation.get("problems") or []),
            "reporting_periods": fp["edition"], "quality": fp["quality"]["summary"]}


def _file_hash(path: Path) -> str | None:
    import hashlib

    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()[:12]
    except OSError:
        return None


def generate_brief(kind: str, as_of: str | None = None, lang: str = "en", publication_id: str | None = None,
                   force: bool = False, db: Database | None = None, narrative_file: str | None = None) -> dict[str, Any]:
    """A brief for one publication: produced when that publication appears, not on a calendar.

    Re-running for the same publication returns `unchanged` unless its extraction or the narrative
    changed, so a repeated download, a translated edition or a backfill does not produce a second
    brief for the same release.
    """
    from .publications.briefs import build_brief
    from .render.briefs import render_brief

    paths = config.paths()
    paths.ensure()
    setup_logging(paths.logs_dir)
    own = db is None
    db = db or Database(paths.db_path)
    as_of_d = parse_as_of(as_of)
    pack = build_brief(db, kind, as_of_d, lang=lang, publication_id=publication_id)
    if pack.get("status") != "ok":
        if own:
            db.close()
        return {"status": "no_publication", "kind": kind, "note": pack.get("note")}
    pub = pack["publication"]
    edition = pub["publication_id"].replace(":", "_")
    prior = [e for e in db.editions(kind) if e["edition_period"] == edition and e["status"] == "generated"]
    if prior and not force:
        try:
            last = json.loads(Path(prior[-1]["manifest_path"]).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            last = {}
        if last.get("fact_pack_hash") == pack["fact_pack_hash"] and last.get("narrative_file") == narrative_file:
            res = {"status": "unchanged", "kind": kind, "publication": pub["publication_id"],
                   "existing": prior[-1]["path"], "note": "this publication has already been briefed and nothing changed"}
            if own:
                db.close()
            return res
    version = _next_version(db, kind, edition)
    ts = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out = _edition_dir(paths, kind, edition, version, ts)
    out.mkdir(parents=True, exist_ok=True)
    stem = f"AZ_{kind}_{edition}_v{version}"
    (out / "fact_pack.json").write_text(json.dumps(pack, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    nar = {}
    validation: dict[str, Any] = {"numbers_checked": 0, "problems": [], "note": "briefs render published values and quoted "
                                                                                "passages; no narrative file was supplied"}
    if narrative_file:
        nar = json.loads(Path(narrative_file).read_text(encoding="utf-8"))
        validation = validate_narrative(nar, pack, evidence_passages(db, {"publications": {"all": [{"publication_id": pub["publication_id"]}]}}))
        nar = flatten(nar)
    (out / "narrative.json").write_text(json.dumps({"narrative": nar, "validation": validation}, ensure_ascii=False,
                                                   indent=1, default=str), encoding="utf-8")
    pptx_path = out / f"{stem}.pptx"
    deck_info = render_brief(kind, pack, nar, pptx_path, lang)
    pdf_info: dict[str, Any] = {"status": "skipped", "reason": "LibreOffice not available"}
    preview_files: list[str] = []
    if soffice_available():
        try:
            pdf = convert_to_pdf(pptx_path, out)
            pdf_info = {"status": "ok", "path": str(pdf)}
            try:
                preview_files = [str(x) for x in previews(pdf, out / "previews")]
            except Exception as exc:
                pdf_info["previews_error"] = str(exc)
        except Exception as exc:
            pdf_info = {"status": "failed", "reason": str(exc)}
    manifest = {
        "report_type": kind, "edition": edition, "version": version, "generated_at": utcnow(), "as_of": as_of_d.isoformat(),
        "publication": pub, "previous_publication": pack.get("previous_publication"),
        "fact_pack_hash": pack["fact_pack_hash"], "narrative_file": narrative_file,
        "narrative_validation": {k: validation.get(k) for k in ("numbers_checked", "problems", "rejected_slides")},
        "status_label": config.settings()["report"]["status_label"],
        "files": {"pptx": str(pptx_path), "pdf": pdf_info, "previews": preview_files,
                  "fact_pack": str(out / "fact_pack.json"), "narrative": str(out / "narrative.json")},
        "slides": deck_info["slides"], "n_slides": deck_info["n_slides"], "language": lang,
        "extraction_notes": pack.get("extraction_notes"),
    }
    manifest["manifest_path"] = str(out / "manifest.json")
    (out / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    db.add_edition({"edition_id": f"{kind}:{edition}:v{version}", "report_type": kind, "edition_period": edition,
                    "version": version, "generated_at": manifest["generated_at"], "as_of": as_of_d.isoformat(),
                    "snapshot_id": None, "status": "generated", "path": str(out),
                    "manifest_path": manifest["manifest_path"], "anchors": json.dumps(pack["edition"])})
    _update_latest(paths, kind, out, manifest)
    res = {"status": "generated", "kind": kind, "publication": pub["publication_id"], "edition": pub.get("edition"),
           "path": str(out), "pptx": str(pptx_path), "pdf": pdf_info, "n_slides": deck_info["n_slides"],
           "reporting_period_end": pub.get("reporting_period_end"), "published_at": pub.get("published_at")}
    if own:
        db.close()
    return res


def generate_report(report_type: str = "monthly", as_of: str | None = None, facts_only: bool = False, narrative_file: str | None = None, lang: str | None = None,
                    since: str | None = None, sector: str | None = None, force: bool = False) -> dict[str, Any]:
    lang = lang or config.settings().get("language", "en")
    if report_type == "monthly":
        return generate_monthly(as_of, facts_only, narrative_file, lang, force=force)
    if report_type == "weekly":
        from .render.weekly import generate_weekly

        return generate_weekly(as_of, since, lang, force=force)
    if report_type == "sector":
        from .render.sector import generate_sector

        return generate_sector(as_of, sector or "agriculture", lang, force=force)
    raise ValueError(report_type)
