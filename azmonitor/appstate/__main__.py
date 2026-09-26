"""`python -m azmonitor.appstate migrate | version | export-ts`"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import export_typescript, migrate, schema_version

TS_PATH = Path(__file__).resolve().parents[2] / "web" / "lib" / "generated" / "appstate-schema.ts"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m azmonitor.appstate")
    sub = ap.add_subparsers(dest="command", required=True)
    sub.add_parser("migrate", help="apply pending application-state migrations")
    sub.add_parser("version", help="print the applied schema version")
    sub.add_parser("export-ts", help="write the migrations for the web application")
    args = ap.parse_args(argv)

    if args.command == "export-ts":
        TS_PATH.parent.mkdir(parents=True, exist_ok=True)
        TS_PATH.write_text(export_typescript(), encoding="utf-8")
        print(json.dumps({"written": str(TS_PATH)}))
        return 0

    from ..cloud import readmodel as RM

    conn = RM.connect()
    try:
        if args.command == "migrate":
            applied = migrate(conn, applied_by="cli")
            print(json.dumps({"applied": applied, "version": schema_version(conn)}))
        else:
            print(json.dumps({"version": schema_version(conn)}))
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
