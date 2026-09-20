"""Running the engine against cloud storage instead of a disk.

The Postgres tests run against a real database when one is reachable and skip when it is not, so a
contributor without Postgres still gets a useful suite and CI still exercises the real thing. They
are not written against a mock: a projection bug that only a real database would catch is exactly
the bug worth catching.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from azmonitor.cloud import lock as L
from azmonitor.cloud import objectstore as OS

PG_URL = os.environ.get("AZMONITOR_TEST_DATABASE_URL") or os.environ.get("AZMONITOR_DATABASE_URL")


def _pg():
    if not PG_URL:
        pytest.skip("no Postgres configured: set AZMONITOR_TEST_DATABASE_URL to run these")
    try:
        from azmonitor.cloud import readmodel as RM

        return RM.connect(PG_URL)
    except Exception as exc:  # pragma: no cover - environment-dependent
        pytest.skip(f"Postgres is not reachable: {exc}")


# --------------------------------------------------------------------- the store

def test_a_key_cannot_escape_the_store(tmp_path):
    """Keys are paths, and a store that accepts `../` is a store that writes anywhere."""
    store = OS.LocalObjectStore(tmp_path / "bucket")
    with pytest.raises(OS.StorageError, match="escapes"):
        store.put("../outside.txt", b"x")
    with pytest.raises(OS.StorageError, match="escapes"):
        store.get("../../etc/passwd")


def test_a_published_edition_is_never_overwritten(tmp_path):
    """An edition version is immutable in the engine, so it is immutable in the store."""
    store = OS.LocalObjectStore(tmp_path / "bucket")
    store.put("reports/monthly/2026-07/v1/deck.pdf", b"first")
    with pytest.raises(OS.StorageError, match="already exists"):
        store.put("reports/monthly/2026-07/v1/deck.pdf", b"second")
    assert store.get("reports/monthly/2026-07/v1/deck.pdf") == b"first"


def test_the_dataset_round_trips_through_the_store(tmp_path):
    data = tmp_path / "data"
    (data / "raw" / "cba").mkdir(parents=True)
    (data / "state").mkdir(parents=True)
    (data / "monitor.sqlite").write_bytes(b"pretend-database" * 100)
    (data / "raw" / "cba" / "table.xlsx").write_bytes(b"source-document")
    (data / "state" / "run_history.json").write_text('{"runs": []}')

    store = OS.LocalObjectStore(tmp_path / "bucket")
    pointer = OS.save_dataset(data, store, stamp="20260920T000000Z")
    assert pointer["key"] == "dataset/monitor-20260920T000000Z.tar.gz"

    fresh = tmp_path / "fresh"
    result = OS.restore_dataset(fresh, store)
    assert result["restored"] is True
    assert (fresh / "monitor.sqlite").read_bytes() == (data / "monitor.sqlite").read_bytes()
    assert (fresh / "raw" / "cba" / "table.xlsx").read_bytes() == b"source-document"
    assert (fresh / "state" / "run_history.json").exists()


def test_a_first_run_finds_nothing_and_says_so(tmp_path):
    """An empty store is the first run, not a failure. The engine's own verification decides."""
    result = OS.restore_dataset(tmp_path / "fresh", OS.LocalObjectStore(tmp_path / "bucket"))
    assert result["restored"] is False and "no dataset pointer" in result["reason"]


def test_a_dataset_that_does_not_match_its_digest_is_refused(tmp_path):
    """A truncated dataset that looks plausible is worse than no dataset."""
    data = tmp_path / "data"
    data.mkdir()
    (data / "monitor.sqlite").write_bytes(b"real-content")
    store = OS.LocalObjectStore(tmp_path / "bucket")
    pointer = OS.save_dataset(data, store, stamp="20260920T000000Z")

    store.put(pointer["key"], b"corrupted", overwrite=True)
    with pytest.raises(OS.StorageError, match="does not match its recorded digest"):
        OS.restore_dataset(tmp_path / "fresh", store)


def test_the_pointer_moves_only_after_the_upload(tmp_path):
    """A run that dies mid-upload must leave the previous dataset current."""
    data = tmp_path / "data"
    data.mkdir()
    (data / "monitor.sqlite").write_bytes(b"v1")
    store = OS.LocalObjectStore(tmp_path / "bucket")
    OS.save_dataset(data, store, stamp="20260920T000000Z")
    first = json.loads(store.get(OS.POINTER_KEY))

    class DiesOnUpload(OS.LocalObjectStore):
        # The dataset goes up through `put_file`, so that 260 MB is never held in memory
        # alongside an open SQLite database. That is the call an interrupted upload interrupts.
        def put_file(self, key, path, *, content_type="application/octet-stream", overwrite=False):
            if key.endswith(".tar.gz"):
                raise OS.StorageError("connection lost half way through the upload")
            return super().put_file(key, path, content_type=content_type, overwrite=overwrite)

    (data / "monitor.sqlite").write_bytes(b"v2")
    dying = DiesOnUpload(tmp_path / "bucket")
    with pytest.raises(OS.StorageError):
        OS.save_dataset(data, dying, stamp="20260920T000100Z")

    assert json.loads(store.get(OS.POINTER_KEY)) == first     # still naming the complete dataset


def test_an_archive_member_cannot_escape_the_data_directory(tmp_path):
    """The archive comes from a store. A member with `../` in its name is an attack, not a bug."""
    import tarfile

    store = OS.LocalObjectStore(tmp_path / "bucket")
    evil = tmp_path / "evil.tar.gz"
    victim = tmp_path / "victim.txt"
    victim.write_text("original")
    with tarfile.open(evil, "w:gz") as tf:
        tf.add(victim, arcname="../victim.txt")
    blob = evil.read_bytes()
    store.put("dataset/evil.tar.gz", blob)
    import hashlib

    store.put(OS.POINTER_KEY, json.dumps({"key": "dataset/evil.tar.gz",
                                          "sha256": hashlib.sha256(blob).hexdigest()}).encode())

    with pytest.raises(OS.StorageError, match="escapes the data directory"):
        OS.restore_dataset(tmp_path / "data", store)
    assert victim.read_text() == "original"


def test_the_store_is_chosen_by_what_is_configured(tmp_path, monkeypatch):
    """A local directory wins over a token, so a test never reaches the network by accident."""
    monkeypatch.setenv("AZMONITOR_OBJECT_STORE_DIR", str(tmp_path / "bucket"))
    monkeypatch.setenv("BLOB_READ_WRITE_TOKEN", "would-be-used-if-no-directory")
    assert isinstance(OS.store_from_env(), OS.LocalObjectStore)

    monkeypatch.delenv("AZMONITOR_OBJECT_STORE_DIR")
    assert isinstance(OS.store_from_env(), OS.VercelBlobStore)

    monkeypatch.delenv("BLOB_READ_WRITE_TOKEN")
    with pytest.raises(OS.StorageError, match="no object store is configured"):
        OS.store_from_env()


# ---------------------------------------------------------------------- the lease

def test_two_workers_cannot_hold_the_same_lease():
    """The case a file lock cannot cover: two machines, each with its own empty disk."""
    a, b = _pg(), _pg()
    from azmonitor.cloud import readmodel as RM

    RM.ensure_schema(a)
    a.execute("DELETE FROM job_locks WHERE name = 'pytest-lease'")
    a.commit()
    try:
        first = L.DatabaseLease(a, "pytest-lease", minutes=5, holder="worker-A")
        first.try_acquire()
        second = L.DatabaseLease(b, "pytest-lease", minutes=5, holder="worker-B")
        with pytest.raises(L.LeaseNotAcquired, match="worker-A"):
            second.try_acquire()
        first.release()
        assert second.try_acquire()["holder"] == "worker-B"
        second.release()
    finally:
        a.execute("DELETE FROM job_locks WHERE name = 'pytest-lease'")
        a.commit()
        a.close()
        b.close()


def test_a_lapsed_lease_is_taken_over_and_its_old_holder_is_told():
    """A worker that died stops renewing. The next run takes over; the dead one, if it wakes, is
    told it no longer holds the lease rather than writing to a dataset someone else owns."""
    a, b = _pg(), _pg()
    from azmonitor.cloud import readmodel as RM

    RM.ensure_schema(a)
    a.execute("DELETE FROM job_locks WHERE name = 'pytest-lease'")
    a.commit()
    try:
        dead = L.DatabaseLease(a, "pytest-lease", minutes=5, holder="worker-that-died")
        dead.try_acquire()
        a.execute("UPDATE job_locks SET expires_at = now() - interval '1 minute' WHERE name = 'pytest-lease'")
        a.commit()

        alive = L.DatabaseLease(b, "pytest-lease", minutes=5, holder="worker-taking-over")
        assert alive.try_acquire()["holder"] == "worker-taking-over"
        with pytest.raises(L.LeaseLost, match="must stop writing"):
            dead.renew()
        alive.release()
    finally:
        a.execute("DELETE FROM job_locks WHERE name = 'pytest-lease'")
        a.commit()
        a.close()
        b.close()


def test_the_holder_names_the_run_an_operator_can_open(monkeypatch):
    monkeypatch.setenv("GITHUB_RUN_ID", "123456")
    monkeypatch.setenv("GITHUB_RUN_ATTEMPT", "2")
    assert L.holder_id() == "github-actions/123456.2"
    monkeypatch.delenv("GITHUB_RUN_ID")
    assert "/" in L.holder_id()


# ----------------------------------------------------------------- the projection

def test_forecasts_stress_paths_and_observations_stay_separate(tmp_path):
    """The distinction the reports maintain must survive into the dashboard's data."""
    from azmonitor.cloud.readmodel import _kind

    assert _kind("month_end_stock", "cba.loans.total_ci") == "observation"
    assert _kind("forecast", "cba.forecast.inflation") == "forecast"
    assert _kind("stress_test_projection", "cba.fsr.stress.car") == "stress"
    assert _kind("policy_rate_effective", "cba.policy.rate") == "policy"


def test_a_reading_held_for_review_is_not_projected(tmp_path):
    """The dashboard must not show a number the deck refused to print."""
    import datetime as dt

    from azmonitor.parsers.base import Observation
    from azmonitor.cloud.readmodel import _rows_from_observations
    from azmonitor.storage.db import Database

    db = Database(tmp_path / "t.sqlite")
    db.store_observations("cba_financial_stability_report", "doc1", [
        Observation(series_id="cba.fsr.car", period_end=dt.date(2025, 12, 31), value=17.6, unit="%",
                    freq="A", period_type="period_end_ratio"),
        Observation(series_id="cba.fsr.car", period_end=dt.date(2023, 12, 31), value=3.0, unit="%",
                    freq="A", period_type="period_end_ratio"),
    ])
    db.conn.execute("UPDATE observations SET validation_status='needs_review' WHERE value = 3.0")
    db.conn.commit()

    values = {r[3] for r in _rows_from_observations(db)}
    assert 17.6 in values and 3.0 not in values
    db.close()


def test_the_projection_matches_the_dataset_it_came_from():
    """Published against a real database and compared back, because a projection that quietly
    disagrees with the dataset is the one failure a dashboard cannot survive."""
    conn = _pg()
    from azmonitor import config
    from azmonitor.cloud import readmodel as RM
    from azmonitor.storage.db import Database

    db_path = config.paths().db_path
    if not db_path.exists():
        pytest.skip("no collected dataset in this working directory")
    db = Database(db_path)
    try:
        RM.ensure_schema(conn)
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM indicators WHERE origin = 'source'")
            projected = cur.fetchone()[0]
        if not projected:
            pytest.skip("the read model has not been published in this environment")

        for series, period in (("cba.loans.total_ci", "2026-07-31"), ("cba.policy.rate", "2026-07-31")):
            src = db.conn.execute(
                "SELECT value FROM observations WHERE series_id=? AND period_end=? AND status='current' "
                "AND COALESCE(validation_status,'verified')='verified' LIMIT 1", (series, period)).fetchone()
            if not src:
                continue
            with conn.cursor() as cur:
                cur.execute("SELECT value FROM indicators WHERE series_id=%s AND period_end=%s "
                            "AND origin='source' LIMIT 1", (series, period))
                got = cur.fetchone()
            assert got is not None, f"{series} is missing from the projection"
            assert abs(got[0] - src[0]) < 1e-9, f"{series} disagrees with the dataset"
    finally:
        db.close()
        conn.close()
