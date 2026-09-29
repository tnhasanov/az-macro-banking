"""The isolated real-cloud event's own preparation: withdraw the newest deposits month from a copy."""
from __future__ import annotations

import importlib.util
import sqlite3
from pathlib import Path

from azmonitor.storage.db import Database

ROOT = Path(__file__).resolve().parents[1]


def _script():
    spec = importlib.util.spec_from_file_location("isolated_event", ROOT / "scripts" / "cloud" / "isolated_event.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _dataset(tmp_path: Path) -> Path:
    db = Database(tmp_path / "monitor.sqlite")
    cols = [r[1] for r in db.conn.execute("PRAGMA table_info(observations)")]
    assert {"dataset_id", "period_end"} <= set(cols)
    db.close()
    conn = sqlite3.connect(tmp_path / "monitor.sqlite")
    conn.execute("INSERT INTO documents(doc_id, source_id, dataset_id, document_url, sha256, retrieved_at, first_seen_at, status) "
                 "VALUES ('d1','cba','cba_deposits','https://example.az/d.xlsx','abc','2026-09-25T00:00:00Z','2026-09-25T00:00:00Z','parsed')")
    required = [r for r in conn.execute("PRAGMA table_info(observations)") if r[3] and r[4] is None and not r[5]]
    for period in ("2026-06-30", "2026-07-31", "2026-08-31"):
        values = {r[1]: "x" for r in required}
        values.update({"dataset_id": "cba_deposits", "period_end": period})
        for r in required:
            if r[2].upper() in ("REAL", "INTEGER", "NUMERIC"):
                values[r[1]] = 1
        values["period_end"] = period
        conn.execute(f"INSERT INTO observations ({', '.join(values)}) VALUES ({', '.join('?' * len(values))})",
                     tuple(values.values()))
    conn.execute("INSERT INTO dataset_state(dataset_id, source_id, latest_period_end) VALUES ('cba_deposits','cba','2026-08-31')")
    conn.commit()
    conn.close()
    return tmp_path


def test_the_newest_deposits_month_is_withdrawn_from_the_copy_only(tmp_path):
    data = _dataset(tmp_path)
    out = _script().withdraw_latest_month(data)
    assert out["withdrawn_period"] == "2026-08-31" and out["now_latest"] == "2026-07-31"
    conn = sqlite3.connect(data / "monitor.sqlite")
    periods = [r[0] for r in conn.execute("SELECT period_end FROM observations ORDER BY 1")]
    assert periods == ["2026-06-30", "2026-07-31"]
    assert conn.execute("SELECT latest_period_end FROM dataset_state WHERE dataset_id='cba_deposits'").fetchone()[0] == "2026-07-31"
    assert conn.execute("SELECT status FROM documents").fetchone()[0] == "superseded", "the next check must read it again"


def test_only_an_isolated_prefix_is_accepted():
    mod = _script()
    assert mod.PREFIX.match("isolated/run-123")
    for bad in ("", "dataset", "isolated/", "isolated/../x", "reports/x", "isolated/Run 1"):
        assert not mod.PREFIX.match(bad), bad


def test_a_log_directory_removed_under_the_handler_never_breaks_a_run(tmp_path):
    import logging

    from azmonitor.util.log import JsonLineHandler

    handler = JsonLineHandler(tmp_path / "work" / "logs" / "monitor.jsonl")
    import shutil

    shutil.rmtree(tmp_path / "work")
    record = logging.LogRecord("azmonitor.test", logging.INFO, __file__, 1, "after the directory went", None, None)
    handler.emit(record)                                    # recreated, not raised
    assert "after the directory went" in (tmp_path / "work" / "logs" / "monitor.jsonl").read_text()


def test_the_override_hook_is_given_a_file_the_fetcher_reads(tmp_path, monkeypatch):
    """The first real trial passed the mapping itself and the fetcher, which expects a file, went to
    the network instead. The script and the fetcher must agree."""
    from azmonitor.ingest.fetch import _override_for

    fixture = tmp_path / "deposits.xlsx"
    fixture.write_bytes(b"PK\x03\x04")
    monkeypatch.setenv("AZMONITOR_FETCH_OVERRIDES", "")      # restored (removed) after the test
    _script().use_overrides(tmp_path, {"https://uploads.cbar.az/assets/x.xlsx": str(fixture)})
    assert _override_for("https://uploads.cbar.az/assets/x.xlsx") == fixture
    assert _override_for("https://uploads.cbar.az/assets/other.xlsx") is None
