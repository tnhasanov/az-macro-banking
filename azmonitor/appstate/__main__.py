"""`python -m azmonitor.appstate migrate | version | preflight | export-ts`"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import export_typescript, migrate, schema_version
from .schema import LATEST

TS_PATH = Path(__file__).resolve().parents[2] / "web" / "lib" / "generated" / "appstate-schema.ts"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m azmonitor.appstate")
    sub = ap.add_subparsers(dest="command", required=True)
    sub.add_parser("migrate", help="apply pending application-state migrations")
    sub.add_parser("version", help="print the applied schema version")
    sub.add_parser("preflight", help="what the application database holds, read-only and without personal data")
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
        elif args.command == "preflight":
            print(json.dumps(preflight(conn), indent=2, default=str))
        else:
            print(json.dumps({"version": schema_version(conn)}))
    finally:
        conn.close()
    return 0


def preflight(conn) -> dict:
    """Facts about the application database a controlled run needs, and nothing personal.

    Workflow logs in this repository are public, so this reports counts and switches, never an
    address, a request or a message. It writes nothing.
    """
    version = schema_version(conn)
    out: dict = {"reachable": True, "schema_version": version, "latest_version": LATEST,
                 "migrations_pending": list(range(version + 1, LATEST + 1))}
    if version == 0:
        return out

    def rows(sql: str) -> list[tuple]:
        with conn.cursor() as cur:
            cur.execute(sql)
            return cur.fetchall()

    out["jobs_by_status"] = {f"{env}/{status}": n for env, status, n in rows(
        "SELECT environment, status, count(*) FROM report_jobs GROUP BY 1, 2 ORDER BY 1, 2")}
    out["publications"] = {env: n for env, n in rows(
        "SELECT environment, count(*) FROM publication_records GROUP BY 1")}
    out["email_by_status"] = {f"{env}/{status}": n for env, status, n in rows(
        "SELECT environment, status, count(*) FROM email_outbox GROUP BY 1, 2")}
    active, suppressed, owners = rows(
        "SELECT count(*) FILTER (WHERE active AND unsubscribed_at IS NULL AND suppressed_at IS NULL), "
        "count(*) FILTER (WHERE suppressed_at IS NOT NULL), count(*) FILTER (WHERE role = 'owner') "
        "FROM recipients")[0]
    out["recipients"] = {"active": active, "suppressed": suppressed, "owners": owners}
    auto_checks = "auto_checks_enabled" if version >= 5 else "false"
    out["switches"] = {env: {"auto_email_enabled": e, "auto_checks_enabled": c, "paused": p}
                       for env, e, c, p in rows(
                           f"SELECT environment, auto_email_enabled, {auto_checks}, paused FROM notification_settings")}
    out["dataset_lease"] = [{"expires_at": e, "expired": x} for e, x in rows(
        "SELECT expires_at, expires_at < now() FROM job_locks WHERE name = 'azmonitor-run'")] \
        if rows("SELECT to_regclass('job_locks')")[0][0] else []
    return out


if __name__ == "__main__":
    sys.exit(main())
