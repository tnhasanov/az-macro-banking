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


def resolve_narrative(fp: dict[str, Any], facts_only_flag: bool, narrative_file: str | None, lang: str) -> tuple[dict[str, Any], dict[str, Any]]:
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
    validation = validate_narrative(nar, fp)
    if nar is not fallback:
        nar = apply_fallback(nar, fallback, validation)
    else:
        nar["validation"] = validation
    # facts-only text is generated from the fact pack itself; a failure here would be a formatting bug and is reported
    if nar is fallback and validation.get("problems"):
        log.warning("facts-only narrative validation reported %d problems", len(validation["problems"]))
    nar["usage"] = usage
    return nar, validation


def generate_monthly(as_of: str | None, facts_only_flag: bool, narrative_file: str | None, lang: str, force: bool = False, db: Database | None = None) -> dict[str, Any]:
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
    prior = [e for e in db.editions("monthly") if e["edition_period"] == edition and e["status"] == "generated"]
    if prior and not force:
        last = json.loads(Path(prior[-1]["manifest_path"]).read_text(encoding="utf-8"))
        if last.get("fact_pack_hash") == fp.get("fact_pack_hash") and last.get("narrative_mode") == ("analyst_file" if narrative_file else ("facts_only" if facts_only_flag else config.settings().get("narrative", {}).get("provider", "none"))):
            res = {"status": "unchanged", "edition": edition, "existing": prior[-1]["path"], "fact_pack_hash": fp.get("fact_pack_hash"), "note": "inputs unchanged since the last edition; use --force to regenerate"}
            if own:
                db.close()
            return res
    version = _next_version(db, "monthly", edition)
    ts = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out = _edition_dir(paths, "monthly", edition, version, ts)
    out.mkdir(parents=True, exist_ok=True)
    stem = f"AZ_Macro_Banking_Monitor_{edition}_v{version}_{ts}"
    (out / "fact_pack.json").write_text(json.dumps(fp, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    nar, validation = resolve_narrative(fp, facts_only_flag, narrative_file, lang)
    (out / "narrative.json").write_text(json.dumps(nar, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    partial = bool(fp["availability"]["missing"])
    # snapshot
    snap = export_snapshot(db, paths.snapshots_dir, paths.analytics_dir, builder.eng.table())
    # deck
    pptx_path = out / f"{stem}.pptx"
    deck_info = render_monthly(fp, nar, pptx_path, lang)
    # workbook
    xlsx_path = build_workbook(fp, nar, db, out / f"{stem}.xlsx", builder.eng.table())
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
        "config_versions": {k: _file_hash(config.CONFIG_DIR / f"{k}.yaml") for k in ("settings", "sources", "metrics", "reports", "theme", "glossary")},
    }
    manifest["manifest_path"] = str(out / "manifest.json")
    (out / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    db.add_edition({"edition_id": f"monthly:{edition}:v{version}", "report_type": "monthly", "edition_period": edition, "version": version, "generated_at": manifest["generated_at"],
                    "as_of": as_of_d.isoformat(), "snapshot_id": snap["snapshot_id"], "status": "generated", "path": str(out), "manifest_path": manifest["manifest_path"],
                    "anchors": json.dumps(fp["edition"])})
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
