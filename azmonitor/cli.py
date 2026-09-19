"""Command-line interface.

    monitor discover
    monitor backfill --start 2020-01
    monitor refresh [--source CBA_MONETARY] [--dataset cba_deposits]
    monitor validate
    monitor status
    monitor fact-pack --report monthly --as-of YYYY-MM-DD
    monitor report --type monthly --as-of YYYY-MM-DD [--facts-only | --narrative-file narrative.json] [--lang en|az]
    monitor report --type weekly --since YYYY-MM-DD
    monitor report --type sector --sector agriculture
    monitor run-due
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def _print(obj) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2, default=str))


def cmd_discover(args) -> int:
    from .pipeline import Pipeline

    p = Pipeline()
    docs = p.discover([args.source] if args.source else None)
    rows = [{"source": d.source_id, "dataset": d.dataset_id, "title": d.title[:70], "url": d.document_url,
             "published_at": d.published_at.isoformat() if d.published_at else None} for d in docs if d.dataset_id or args.all]
    _print({"n_links": len(docs), "matched": rows})
    return 0


def cmd_refresh(args) -> int:
    from .pipeline import Pipeline

    p = Pipeline()
    summary = p.refresh(source_ids=[args.source] if args.source else None, dataset_ids=[args.dataset] if args.dataset else None,
                        history_start=getattr(args, "start", None), recent=getattr(args, "recent", None))
    _print({k: {kk: vv for kk, vv in v.items() if kk != "warnings"} | {"n_warnings": len(v["warnings"])} for k, v in summary["datasets"].items()})
    errors = sum(len(v["errors"]) for v in summary["datasets"].values())
    return 0 if errors == 0 else 2


def cmd_reparse(args) -> int:
    from .pipeline import Pipeline

    p = Pipeline(offline=True)
    _print(p.reparse([args.dataset] if args.dataset else None))
    return 0


def cmd_validate(args) -> int:
    from .calc.validate import validate_all

    rep = validate_all()
    _print(rep["summary"])
    out = Path(rep["path"])
    print(f"quality report: {out}", file=sys.stderr)
    return 0 if rep["summary"]["failed"] == 0 else 2


def cmd_status(args) -> int:
    from .pipeline import Pipeline

    p = Pipeline(offline=True)
    states = {k: dict(v) for k, v in p.db.dataset_states().items()}
    _print({"data_dir": str(p.paths.data_dir), "datasets": states, "editions": [dict(e) for e in p.db.editions()][-5:]})
    return 0


def cmd_fact_pack(args) -> int:
    from .facts import build_fact_pack

    fp, path = build_fact_pack(report_type=args.report, as_of=args.as_of, lang=args.lang)
    print(f"fact pack: {path}", file=sys.stderr)
    _print({"as_of": fp["as_of"], "anchors": fp["anchors"], "n_metrics": len(fp["metrics"]), "missing": fp["availability"]["missing"]})
    return 0


def cmd_report(args) -> int:
    from .reports import generate_report

    if args.type in ("mpr-brief", "fsr-brief", "decision-update"):
        from . import config
        from .reports import generate_brief

        kind = {"mpr-brief": "mpr_brief", "fsr-brief": "fsr_brief", "decision-update": "decision_update"}[args.type]
        res = generate_brief(kind, as_of=args.as_of, lang=args.lang or config.settings().get("language", "en"),
                             publication_id=getattr(args, "publication", None), force=args.force,
                             narrative_file=args.narrative_file)
        _print(res)
        return 0 if res.get("status") in ("generated", "unchanged", "no_publication") else 2
    res = generate_report(report_type=args.type, as_of=args.as_of, facts_only=args.facts_only, narrative_file=args.narrative_file,
                          lang=args.lang, since=args.since, sector=args.sector, force=args.force)
    _print(res)
    return 0 if res.get("status") in ("generated", "no_update", "unchanged") else 2


def cmd_commentary_pack(args) -> int:
    """Write the pack a Claude Code commentary session works from (no API call involved)."""
    import datetime as dt

    from . import config
    from .facts import build_fact_pack
    from .narrative.commentary import write_request
    from .reports import evidence_passages
    from .storage.db import Database

    paths = config.paths()
    db = Database(paths.db_path)
    fp, _ = build_fact_pack(report_type=args.type, as_of=args.as_of, db=db, write=True)
    out = Path(args.out) if args.out else paths.state_dir / f"commentary_request_{fp['edition']['edition_month']}.json"
    write_request(fp, evidence_passages(db, fp), out)
    _print({"status": "ok", "request": str(out), "edition": fp["edition"], "fact_pack_hash": fp["fact_pack_hash"],
            "claims_available": len(json.loads(out.read_text())["claim_catalogue"]),
            "next": f"write narratives/monthly_{fp['edition']['edition_month']}_analyst.json, then "
                    f"`monitor report --type monthly --narrative-file <path>`"})
    db.close()
    return 0


def cmd_bind_claims(args) -> int:
    """Propose claim ids for the numbers in an existing narrative and report what needs a decision."""
    from . import config
    from .facts import build_fact_pack
    from .narrative.commentary import bind_claims
    from .storage.db import Database

    paths = config.paths()
    db = Database(paths.db_path)
    fp, _ = build_fact_pack(report_type="monthly", as_of=args.as_of, db=db, write=False)
    nar = json.loads(Path(args.narrative).read_text(encoding="utf-8"))
    bound, report = bind_claims(nar, fp)
    out = Path(args.out or args.narrative)
    if not args.dry_run:
        out.write_text(json.dumps(bound, ensure_ascii=False, indent=1), encoding="utf-8")
    _print({"status": "ok", "written": None if args.dry_run else str(out), "bound": report["bound"],
            "ambiguous": report["ambiguous"][:20], "unsupported": report["unsupported"][:20],
            "n_ambiguous": len(report["ambiguous"]), "n_unsupported": len(report["unsupported"])})
    db.close()
    return 0 if not report["unsupported"] else 2


def cmd_run_due(args) -> int:
    from .scheduling.run_due import run_due

    res = run_due(dry_run=args.dry_run)
    _print(res)
    # the exit code says what happened, so a scheduler can alert on the right thing
    return {"ok": 0, "partial": 2, "failed": 3, "skipped_locked": 4, "failed_restore": 5, "failed_save": 6}.get(
        res.get("status", "failed"), 3)


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="monitor", description="Azerbaijan Macro & Banking Monitor")
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("discover", help="Resolve current official document links from the entry pages")
    s.add_argument("--source")
    s.add_argument("--all", action="store_true", help="include unmatched links")
    s.set_defaults(fn=cmd_discover)

    s = sub.add_parser("backfill", help="Download and parse all available history from --start")
    s.add_argument("--start", default=None, help="YYYY-MM (default from settings.history_start)")
    s.add_argument("--source")
    s.add_argument("--dataset")
    s.set_defaults(fn=cmd_refresh)

    s = sub.add_parser("refresh", help="Check sources, download changed files, parse and store new vintages")
    s.add_argument("--source")
    s.add_argument("--dataset")
    s.add_argument("--recent", type=int, help="For multi-edition sources, process only the N most recent editions "
                                              "(staged backfill: validate the latest editions before loading the archive)")
    s.set_defaults(fn=cmd_refresh)

    s = sub.add_parser("reparse", help="Re-run parsers on stored documents (after a parser fix); new vintages only where values change")
    s.add_argument("--dataset")
    s.set_defaults(fn=cmd_reparse)

    s = sub.add_parser("validate", help="Run data-quality checks on the current dataset")
    s.set_defaults(fn=cmd_validate)

    s = sub.add_parser("status", help="Show dataset state and recent editions")
    s.set_defaults(fn=cmd_status)

    s = sub.add_parser("fact-pack", help="Build the structured fact pack for a report")
    s.add_argument("--report", default="monthly", choices=["monthly", "weekly", "sector"])
    s.add_argument("--as-of", default=None)
    s.add_argument("--lang", default=None)
    s.set_defaults(fn=cmd_fact_pack)

    s = sub.add_parser("report", help="Generate a report edition (pptx, pdf, xlsx, fact pack, narrative, manifest)")
    s.add_argument("--type", default="monthly",
                   choices=["monthly", "weekly", "sector", "mpr-brief", "fsr-brief", "decision-update"])
    s.add_argument("--publication", help="Publication id for a brief (default: the most recent one of that type)")
    s.add_argument("--as-of", default=None)
    s.add_argument("--facts-only", action="store_true")
    s.add_argument("--narrative-file", default=None)
    s.add_argument("--lang", default=None)
    s.add_argument("--since", default=None, help="weekly: report releases since this date")
    s.add_argument("--sector", default=None, help="sector review: agriculture|construction|trade|transport|industry")
    s.add_argument("--force", action="store_true", help="generate even if inputs are unchanged since the last edition")
    s.set_defaults(fn=cmd_report)

    s = sub.add_parser("commentary-pack", help="Write the commentary request a Claude Code session drafts from "
                                               "(claim catalogue, quotable passages, rules); no API call is made")
    s.add_argument("--type", default="monthly")
    s.add_argument("--as-of", default=None)
    s.add_argument("--out", default=None)
    s.set_defaults(fn=cmd_commentary_pack)

    s = sub.add_parser("bind-claims", help="Propose claim ids for the numbers in a narrative file and report the "
                                           "ambiguous and unsupported ones")
    s.add_argument("--narrative", required=True)
    s.add_argument("--as-of", default=None)
    s.add_argument("--out", default=None)
    s.add_argument("--dry-run", action="store_true")
    s.set_defaults(fn=cmd_bind_claims)

    s = sub.add_parser("run-due", help="Scheduler entry point: check sources and generate due reports (idempotent, locked)")
    s.add_argument("--dry-run", action="store_true")
    s.set_defaults(fn=cmd_run_due)
    return ap


def main(argv: list[str] | None = None) -> int:
    ap = build_parser()
    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
