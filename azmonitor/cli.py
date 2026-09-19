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
                        history_start=getattr(args, "start", None))
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

    res = generate_report(report_type=args.type, as_of=args.as_of, facts_only=args.facts_only, narrative_file=args.narrative_file,
                          lang=args.lang, since=args.since, sector=args.sector, force=args.force)
    _print(res)
    return 0 if res.get("status") in ("generated", "no_update", "unchanged") else 2


def cmd_run_due(args) -> int:
    from .scheduling.run_due import run_due

    res = run_due(dry_run=args.dry_run)
    _print(res)
    return 0 if res.get("status") != "failed" else 2


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
    s.add_argument("--type", default="monthly", choices=["monthly", "weekly", "sector"])
    s.add_argument("--as-of", default=None)
    s.add_argument("--facts-only", action="store_true")
    s.add_argument("--narrative-file", default=None)
    s.add_argument("--lang", default=None)
    s.add_argument("--since", default=None, help="weekly: report releases since this date")
    s.add_argument("--sector", default=None, help="sector review: agriculture|construction|trade|transport|industry")
    s.add_argument("--force", action="store_true", help="generate even if inputs are unchanged since the last edition")
    s.set_defaults(fn=cmd_report)

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
