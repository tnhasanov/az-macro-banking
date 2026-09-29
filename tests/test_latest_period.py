""""Latest available" is read from the data every time, never from a configured or remembered month.

The end-to-end run produced a July monthly from a release published on 25 September, which is worth
explaining rather than leaving to look like a pinned period. The monthly edition month is the
earliest of the latest periods held for its banking anchors (config/reports.yaml: monthly.anchors.
banking = cba_loans_by_institution and cba_deposits). CBA's 25 September deposits file carried
July and August; the loans-by-institution table still ended in July. So the common banking month
was July. The run's seed had withdrawn July deposits on purpose, which is why its first "latest"
request (J1) produced June and the check that downloaded the live file then produced July (J5).
When CBA publishes August loans, the same request produces August, with nothing changed here.
"""
from __future__ import annotations

import datetime as dt

import pytest

from azmonitor import config
from azmonitor.jobs.engine import Engine
from azmonitor.storage.db import Database


@pytest.fixture
def engine(tmp_path, monkeypatch):
    monkeypatch.setenv("AZMONITOR_DATA_DIR", str(tmp_path / "data"))
    (tmp_path / "data").mkdir()
    e = Engine()
    e.db = Database(config.paths().db_path)
    yield e
    e.db.close()


def _held(engine, **ends):
    for dataset_id, end in ends.items():
        engine.db.set_dataset_state(dataset_id, source_id=dataset_id.split("_")[0], latest_period_end=end,
                                    status="ok")


def test_the_edition_month_is_the_common_latest_banking_period(engine):
    _held(engine, cba_loans_by_institution="2026-07-31", cba_deposits="2026-08-31",
          ssc_macro_headline_html="2026-08-31", ssc_price_bulletin="2026-08-31")
    assert engine.banking_month() == "2026-07", "deposits ahead of loans: the month both cover"


def test_it_moves_on_as_soon_as_the_lagging_table_does(engine):
    _held(engine, cba_loans_by_institution="2026-08-31", cba_deposits="2026-08-31")
    assert engine.banking_month() == "2026-08"
    _held(engine, cba_loans_by_institution="2026-09-30", cba_deposits="2026-09-30")
    assert engine.banking_month() == "2026-09"


def test_the_june_then_july_of_the_end_to_end_run(engine):
    _held(engine, cba_loans_by_institution="2026-07-31", cba_deposits="2026-06-30")   # the seed
    assert engine.banking_month() == "2026-06"
    _held(engine, cba_deposits="2026-08-31")                                            # the live file
    assert engine.banking_month() == "2026-07"


def test_without_every_banking_table_there_is_no_latest_month_rather_than_a_guess(engine):
    _held(engine, cba_deposits="2026-08-31")
    assert engine.banking_month() is None


def test_availability_reports_each_role_from_the_data(engine):
    _held(engine, cba_loans_by_institution="2026-07-31", cba_deposits="2026-08-31",
          ssc_macro_headline_html="2026-08-31", ssc_price_bulletin="2026-08-31")
    a = engine.availability(dt.date(2026, 9, 26))
    assert a["monthly"]["edition_month"] == "2026-07"
    assert a["monthly"]["anchors"]["banking"]["period_end"] == "2026-07-31"
    assert a["monthly"]["anchors"]["prices"]["period_end"] == "2026-08-31"


def test_no_period_is_pinned_anywhere_in_configuration_or_code():
    import pathlib
    import re

    root = pathlib.Path(__file__).resolve().parents[1]
    pinned = re.compile(r"""["']20\d\d-\d\d(-\d\d)?["']""")
    # Dates that are not reporting periods, each for a stated reason.
    allowed = (
        "history_start",           # the first month collected, a lower bound for history
        "x-github-api-version",    # the REST API version GitHub is asked to speak
        '"period": "2026-07-31", "comparison"',   # the shape of a claim, shown to the narrative writer
    )
    offenders = []
    for path in [*root.glob("azmonitor/**/*.py"), *root.glob("config/*.yaml"),
                 *root.glob("web/lib/**/*.ts"), *root.glob("web/app/**/*.ts*")]:
        if "generated" in path.parts:
            continue
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if pinned.search(line) and not line.lstrip().startswith(("#", "//", "*", '"""')) \
                    and not any(a in line for a in allowed):
                offenders.append(f"{path.relative_to(root)}:{n}: {line.strip()[:100]}")
    assert offenders == [], "a literal period in code or configuration:\n" + "\n".join(offenders)


# ------------------------------------------------------------------------ the stated information cutoff

def _checked(engine, dataset_id, when, status="unchanged"):
    engine.db.set_dataset_state(dataset_id, source_id=dataset_id.split("_")[0], last_checked_at=when, status=status)


def test_the_cutoff_is_the_last_successful_collection_not_the_generation_date(engine):
    """A snapshot collected on 20 September must not present itself as information to today."""
    for d in ("cba_loans_by_institution", "cba_deposits", "ssc_macro_headline_html", "ssc_price_bulletin"):
        _checked(engine, d, "2026-09-20T09:26:00+00:00")
    _checked(engine, "ssc_price_bulletin", "2026-09-20T09:29:33+00:00")
    assert engine.information_date() == "2026-09-20"


def test_a_failed_check_does_not_move_the_cutoff(engine):
    """CBA's files could not be downloaded: the check stamps its time, but nothing was collected."""
    for d in ("cba_loans_by_institution", "cba_deposits", "ssc_macro_headline_html", "ssc_price_bulletin"):
        _checked(engine, d, "2026-09-27T05:40:00+00:00")
    engine.db.conn.execute(
        "INSERT INTO documents(doc_id, source_id, dataset_id, document_url, sha256, retrieved_at, first_seen_at) "
        "VALUES ('d', 'cba', 'cba_deposits', 'https://example.az/d.xlsx', 'x', '2026-09-18T22:06:47+00:00', "
        "'2026-09-18T22:06:47+00:00')")
    engine.db.conn.commit()
    _checked(engine, "cba_deposits", "2026-09-27T05:40:00+00:00", status="fetch_failed")
    assert engine.information_date() == "2026-09-19", "the last deposits file actually read, in Baku time"


def test_no_cutoff_is_claimed_for_a_source_never_read(engine):
    _checked(engine, "cba_deposits", "2026-09-20T09:26:00+00:00")
    assert engine.information_date() is None
