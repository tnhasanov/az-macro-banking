"""From a rendered edition on disk to something that may be published.

Three steps, in order, and publication happens only after all three:

  validate  the files the engine wrote are the files the edition claims, the PDF opens and has the
            deck's pages, the narrative is grounded, nothing branded is about to leave the building;
  upload    every artefact goes to private storage under a path that belongs to this job attempt
            alone, and each upload is confirmed by reading back its size;
  describe  the publication record: which files, their digests, the reporting period of every
            input, the information cutoff, the grounded findings and the stated limitations.

A path per job attempt is what makes the upload immutable in practice. Two workers that both
believed they were producing version 4 — one of them a zombie whose lease had lapsed — write to
different prefixes, and the publication record names exactly one of them.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from .. import config
from ..util.log import get_logger

log = get_logger("jobs.outputs")

DOWNLOADABLE = {".pdf": "pdf", ".pptx": "pptx", ".xlsx": "xlsx"}
CONTENT_TYPES = {".pdf": "application/pdf",
                 ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
                 ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                 ".json": "application/json"}
BAKU = ZoneInfo("Asia/Baku")


class ValidationFailed(RuntimeError):
    def __init__(self, checks: list[dict[str, Any]]):
        self.checks = checks
        failed = [c for c in checks if not c["ok"] and c.get("blocking", True)]
        super().__init__("; ".join(f"{c['id']}: {c['detail']}" for c in failed[:4]))


def _check(cid: str, ok: bool, detail: str, blocking: bool = True) -> dict[str, Any]:
    return {"id": cid, "ok": bool(ok), "detail": detail, "blocking": blocking}


def read_manifest(edition_dir: Path) -> dict[str, Any]:
    try:
        return json.loads((edition_dir / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _pdf_pages(path: Path) -> int | None:
    try:
        import pdfplumber

        with pdfplumber.open(path) as pdf:
            return len(pdf.pages)
    except Exception:  # an unreadable PDF is a failed check, reported by the caller
        return None


def validate(report_type: str, result: dict[str, Any], *, profile_allows_upload: bool) -> list[dict[str, Any]]:
    """Every check an edition must pass before it may be published. Raises ValidationFailed."""
    checks: list[dict[str, Any]] = []
    edition_dir = Path(result.get("path") or "")
    manifest = read_manifest(edition_dir) if result.get("path") else {}

    checks.append(_check("manifest", bool(manifest) and int(manifest.get("version") or -1) == int(result.get("version") or -2),
                         "the engine's manifest is present and names this version" if manifest
                         else "the edition has no readable manifest"))

    pptx = Path(result.get("pptx") or "")
    checks.append(_check("deck", pptx.is_file() and pptx.stat().st_size > 10_000,
                         f"{pptx.name} ({pptx.stat().st_size if pptx.is_file() else 0} bytes)"))

    pdf_info = result.get("pdf") or {}
    pdf = Path(pdf_info.get("path") or "")
    if pdf_info.get("status") != "ok" or not pdf.is_file():
        checks.append(_check("pdf", False, f"no PDF was rendered ({pdf_info.get('reason') or pdf_info.get('status')})"))
    else:
        pages = _pdf_pages(pdf)
        n_slides = result.get("n_slides") or manifest.get("n_slides")
        ok = pages is not None and pages > 0 and (not n_slides or pages == int(n_slides))
        checks.append(_check("pdf", ok, f"{pdf.name}: {pages} page(s) for {n_slides} slide(s)" if pages is not None
                             else f"{pdf.name} could not be opened"))

    if report_type == "monthly":
        xlsx = Path(result.get("xlsx") or "")
        checks.append(_check("evidence_workbook", xlsx.is_file() and xlsx.stat().st_size > 5_000,
                             f"{xlsx.name} ({xlsx.stat().st_size if xlsx.is_file() else 0} bytes)"))

    nv = manifest.get("narrative_validation") or {}
    problems = nv.get("problems") or []
    if manifest.get("narrative_mode") == "facts_only" or report_type != "monthly":
        # Text generated from the fact pack itself, or published values quoted with their pages:
        # any problem here means a number in the deck is not the number in the fact pack.
        checks.append(_check("grounding", not problems,
                             f"{nv.get('numbers_checked') or 0} number(s) checked, {len(problems)} problem(s)"))
    else:
        # An authored or model narrative: rejected statements were replaced by the facts-only text
        # before rendering, so what is in the deck is grounded; the replacements are disclosed.
        rejected = len(nv.get("rejected_findings") or []) + len(nv.get("rejected_slides") or [])
        checks.append(_check("grounding", True,
                             f"{nv.get('numbers_checked') or 0} number(s) checked; {rejected} statement(s) "
                             f"replaced by the facts-only text before rendering"))

    quality = manifest.get("quality_summary") or {}
    if quality:
        checks.append(_check("quality", int(quality.get("critical") or 0) == 0 or bool(quality.get("accepted_exceptions")),
                             f"{quality.get('checks', 0)} checks, {quality.get('critical', 0)} critical"))

    uploadable = files_to_upload(edition_dir) if edition_dir.is_dir() else []
    if profile_allows_upload:
        checks.append(_check("distribution", True, f"profile {config.profile()['name']} permits upload"))
    else:
        from ..cloud import artifacts

        findings = artifacts.screen(uploadable)
        checks.append(_check("distribution", not findings,
                             "no organisation branding found in the artefacts" if not findings
                             else f"{len(findings)} file(s) carry organisation branding under a profile "
                                  "that does not permit external upload"))

    if any(not c["ok"] and c["blocking"] for c in checks):
        raise ValidationFailed(checks)
    return checks


def files_to_upload(edition_dir: Path) -> list[Path]:
    return [f for f in sorted(edition_dir.iterdir())
            if f.is_file() and not f.name.startswith(".") and f.suffix in (".pdf", ".pptx", ".xlsx", ".json")]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def upload(store, edition_dir: Path, prefix: str, *, fence=None) -> list[dict[str, Any]]:
    """Upload every artefact under `prefix` and confirm each one by reading its size back."""
    out: list[dict[str, Any]] = []
    for path in files_to_upload(edition_dir):
        if fence is not None:
            fence.check(f"uploading {path.name}")
        key = f"{prefix}/{path.name}"
        size = path.stat().st_size
        entry = {"name": path.name, "key": key, "bytes": size, "sha256": _sha256(path),
                 "content_type": CONTENT_TYPES.get(path.suffix, "application/octet-stream"),
                 "role": DOWNLOADABLE.get(path.suffix, "evidence" if path.name != "manifest.json" else "manifest")}
        existing = store.stat(key)
        if existing is None:
            store.put_file(key, path, content_type=entry["content_type"])
            existing = store.stat(key)
        if existing is None:
            raise RuntimeError(f"{key} was uploaded but the store does not list it")
        if existing.get("size") not in (None, size):
            raise RuntimeError(f"{key}: the store holds {existing.get('size')} bytes where {size} were sent")
        entry["verified_at"] = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
        out.append(entry)
    if not any(f["role"] == "pdf" for f in out):
        raise RuntimeError("no PDF among the uploaded files")
    return out


# ------------------------------------------------------------------ the publication record

def _text(value: Any) -> str:
    if isinstance(value, dict):
        return str(value.get("text") or value.get("en") or "")
    return str(value or "")


def brief_findings(pack: dict[str, Any], report_type: str, limit: int = 5) -> list[str]:
    """Findings for a brief, built from published values with their citations and nothing else."""
    out: list[str] = []
    policy = pack.get("policy") or {}
    stability = pack.get("stability") or {}

    def value_line(item: dict[str, Any], prefix: str = "") -> str | None:
        if item.get("value") is None or not item.get("definition"):
            return None
        when = item.get("observation_date") or ""
        cite = item.get("source_citation")
        return (f"{prefix}{item['definition'][:1].upper()}{item['definition'][1:]}: {item['value']:g}{item.get('unit') or ''}"
                f"{' for ' + when[:7] if when else ''}{' (' + cite + ')' if cite else ''}.")

    if report_type == "decision_update":
        stance = (policy.get("stance") or {}).get("statement")
        if stance:
            out.append(stance)
        corridor = policy.get("corridor_now") or {}
        if corridor.get("floor") is not None:
            out.append(f"Interest rate corridor: floor {corridor['floor']:g}%, refinancing rate "
                       f"{corridor.get('rate'):g}%, ceiling {corridor.get('ceiling'):g}%.")
        nxt = policy.get("next_decision") or {}
        if isinstance(nxt, dict) and nxt.get("date"):
            out.append(f"Next scheduled decision: {nxt['date']}.")
    elif report_type == "mpr_brief":
        fc = policy.get("forecasts") or {}
        for item in (fc.get("current") or [])[:4]:
            line = value_line(item, "CBA projection: ")
            if line:
                out.append(line)
    elif report_type == "fsr_brief":
        for item in (stability.get("dashboard") or [])[:3]:
            line = value_line(item)
            if line:
                out.append(line)
        for item in ((stability.get("stress_tests") or {}).get("results") or [])[:1]:
            line = value_line(item, f"Stress test ({item.get('scenario') or 'scenario'}): ")
            if line:
                out.append(line)
    return out[:limit]


def findings_for(report_type: str, edition_dir: Path, manifest: dict[str, Any]) -> list[str]:
    if report_type in ("mpr_brief", "fsr_brief", "decision_update"):
        try:
            pack = json.loads((edition_dir / "fact_pack.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        return brief_findings(pack, report_type)
    try:
        narrative = json.loads((edition_dir / "narrative.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    from ..delivery.compose import _findings

    return _findings(manifest, narrative, limit=5)


def limitations_for(report_type: str, manifest: dict[str, Any]) -> list[str]:
    from ..delivery.compose import _partial_note, _quality_note

    out = [n for n in (_partial_note(manifest), _quality_note(manifest)) if n]
    nv = manifest.get("narrative_validation") or {}
    rejected = len(nv.get("rejected_findings") or []) + len(nv.get("rejected_slides") or [])
    if rejected:
        out.append(f"{rejected} authored statement(s) failed the grounding check and were replaced by "
                   "descriptive text generated from the data.")
    if manifest.get("narrative_mode") == "facts_only":
        out.append("The commentary is descriptive: it states the published values and asserts no "
                   "interpretation beyond them.")
    rp = manifest.get("reporting_periods") or {}
    distinct = {str(v)[:7] for k, v in rp.items() if v and k.endswith(("period", "_period_end", "month"))}
    if len(distinct) > 1:
        out.append("Inputs cover different periods because banking tables, prices and national accounts "
                   "are published on different calendars; each slide states its own period.")
    if report_type == "weekly" and not manifest.get("new_documents"):
        out.append("Nothing new was published in this window; the digest records that explicitly.")
    return out


def information_cutoff(manifest: dict[str, Any]) -> str | None:
    as_of = manifest.get("as_of")
    if not as_of:
        return None
    try:
        day = dt.date.fromisoformat(str(as_of)[:10])
    except ValueError:
        return None
    return dt.datetime.combine(day, dt.time(23, 59, 59), BAKU).isoformat()


def build_record(*, report_type: str, result: dict[str, Any], params: dict[str, Any], files: list[dict[str, Any]],
                 checks: list[dict[str, Any]], prefix: str, fingerprint_detail: dict[str, Any] | None,
                 scope_key: str) -> dict[str, Any]:
    edition_dir = Path(result["path"])
    manifest = read_manifest(edition_dir)
    edition = result.get("edition_key") if report_type in ("mpr_brief", "fsr_brief", "decision_update") else result.get("edition")
    edition = edition or manifest.get("edition")
    version = int(result.get("version") or manifest.get("version"))
    return {
        "edition_id": result.get("edition_id") or f"{report_type}:{edition}:v{version}",
        "report_type": report_type, "edition": edition, "version": version,
        "sector": params.get("sector"), "scope_key": scope_key,
        "fingerprint": result.get("fingerprint") or (fingerprint_detail or {}).get("fingerprint"),
        "manifest": {
            "prefix": prefix, "files": files,
            "n_slides": result.get("n_slides") or manifest.get("n_slides"),
            "language": manifest.get("language"), "generated_at": manifest.get("generated_at"),
            "as_of": manifest.get("as_of"), "status_label": manifest.get("status_label"),
            "partial": bool(manifest.get("partial_edition")), "narrative_mode": manifest.get("narrative_mode"),
            "publication": manifest.get("publication"), "window": manifest.get("window"),
            "fingerprint_detail": fingerprint_detail, "trigger": manifest.get("edition_trigger"),
            "quality_summary": manifest.get("quality_summary") or {},
            "numbers_checked": (manifest.get("narrative_validation") or {}).get("numbers_checked"),
        },
        "validation": {"checks": checks, "narrative": manifest.get("narrative_validation") or {}},
        "reporting_periods": manifest.get("reporting_periods") or {},
        "information_cutoff": information_cutoff(manifest),
        "findings": findings_for(report_type, edition_dir, manifest),
        "limitations": limitations_for(report_type, manifest),
    }


def catalog_entry(record: dict[str, Any]) -> dict[str, Any]:
    """The dashboard's archive row, and the durable catalogue object beside the files."""
    m = record["manifest"]
    return {
        "report_type": record["report_type"], "edition": record["edition"], "version": record["version"],
        "edition_id": record["edition_id"],
        "generated_at": m.get("generated_at"), "as_of": m.get("as_of"), "status_label": m.get("status_label"),
        "partial": m.get("partial"), "n_slides": m.get("n_slides"), "fingerprint": record.get("fingerprint"),
        "trigger": (m.get("trigger") or {}).get("trigger") if isinstance(m.get("trigger"), dict) else None,
        "narrative_mode": m.get("narrative_mode"), "numbers_checked": m.get("numbers_checked"),
        "quality": m.get("quality_summary") or {}, "reporting_periods": record.get("reporting_periods") or {},
        "summary": record.get("findings") or [],
        "files": [{"name": f["name"], "bytes": f["bytes"], "key": f["key"]} for f in m["files"]
                  if f["role"] in ("pdf", "pptx", "xlsx")],
        "blob_prefix": m["prefix"],
    }
