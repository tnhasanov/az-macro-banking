"""A fresh application-state schema per test, on a real Postgres.

Each test gets its own Postgres schema with the migrations applied, so tests are isolated from one
another and from any database a developer is actually using. Skipped when no test database is
configured; CI provides one.
"""
from __future__ import annotations

import os
import secrets

import pytest

PG_URL = os.environ.get("AZMONITOR_TEST_DATABASE_URL")


def connect_in(schema: str):
    import psycopg

    conn = psycopg.connect(PG_URL, options=f"-c search_path={schema}")
    conn.autocommit = True
    return conn


@pytest.fixture
def pg():
    if not PG_URL:
        pytest.skip("AZMONITOR_TEST_DATABASE_URL is not set")
    import psycopg

    schema = "t_" + secrets.token_hex(6)
    admin = psycopg.connect(PG_URL, autocommit=True)
    admin.execute(f"CREATE SCHEMA {schema}")
    conn = connect_in(schema)
    from azmonitor.appstate import migrate
    from azmonitor.cloud import readmodel as RM

    RM.ensure_schema(conn)
    migrate(conn, applied_by="test")
    conn.schema = schema                       # type: ignore[attr-defined]
    conn.connect_again = lambda: connect_in(schema)  # type: ignore[attr-defined]
    try:
        yield conn
    finally:
        conn.close()
        admin.execute(f"DROP SCHEMA {schema} CASCADE")
        admin.close()


def add_recipient(conn, email: str, report_types=("monthly",), *, sector: str = "*",
                  role: str = "subscriber") -> str:
    from azmonitor.appstate import new_id

    rid = new_id("rcp")
    conn.execute("INSERT INTO recipients(recipient_id, email, role) VALUES (%s,%s,%s)", (rid, email, role))
    for rt in report_types:
        conn.execute("INSERT INTO subscriptions(recipient_id, report_type, sector) VALUES (%s,%s,%s)",
                     (rid, rt, sector))
    return rid


def settings(conn, environment: str = "production", **values) -> None:
    cols = {"auto_email_enabled": True, "paused": False, "notify_revisions": True, **values}
    conn.execute(
        "INSERT INTO notification_settings(environment, auto_email_enabled, paused, notify_revisions, "
        "revision_settle_minutes) VALUES (%s,%s,%s,%s,%s) ON CONFLICT (environment) DO UPDATE SET "
        "auto_email_enabled = EXCLUDED.auto_email_enabled, paused = EXCLUDED.paused, "
        "notify_revisions = EXCLUDED.notify_revisions, revision_settle_minutes = EXCLUDED.revision_settle_minutes",
        (environment, cols["auto_email_enabled"], cols["paused"], cols["notify_revisions"],
         cols.get("revision_settle_minutes", 120)))
