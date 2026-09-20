"""Extraction and handling of CBA narrative publications, on real source fixtures."""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import pytest

from azmonitor.publications import policy as pol
from azmonitor.publications import stability as stab
from azmonitor.publications.editions import classify_title, decision_edition
from azmonitor.publications.extract import extract_press_release
from azmonitor.publications.factpack import PublicationFacts
from azmonitor.publications.review import review_publication_series
from azmonitor.storage.db import Database

FIXTURES = Path(__file__).parent / "fixtures"


def _passages(text: str, kind: str = "text", page: int = 1) -> list[dict]:
    return [{"kind": kind, "text": text, "page_index": page, "printed_page": str(page),
             "passage_id": f"fixture#{page}", "validation_status": "verified"}]


# --------------------------------------------------------------------- editions

@pytest.mark.parametrize("pub_type,title,key,start,end", [
    ("monetary_policy_review", "Pul Siyasəti İcmalı – Avqust 2026", "2026-08", None, None),
    ("monetary_policy_review", "Monetary policy review – August 2026", "2026-08", None, None),
    ("monetary_policy_review", "Monetary policy review, January - June 2024", "2024-06", "2024-01-01", "2024-06-30"),
    ("financial_stability_report", "Financial Stability Report for Half 1, 2025", "2025-H1", "2025-01-01", "2025-06-30"),
    ("financial_stability_report", "Maliyyə sabitliyi hesabatı - 2025", "2025-A", "2025-01-01", "2025-12-31"),
    ("policy_directions", "Azərbaycan Respublikası Mərkəzi Bankının 2022-ci il üçün pul və maliyyə sabitliyi siyasəti",
     "2022-Y", "2022-01-01", "2022-12-31"),
])
def test_both_language_editions_resolve_to_the_same_edition_key(pub_type, title, key, start, end):
    info = classify_title(pub_type, title)
    assert info is not None and info.edition_key == key
    assert (info.reporting_start.isoformat() if info.reporting_start else None) == start
    assert (info.reporting_end.isoformat() if info.reporting_end else None) == end


def test_review_with_no_stated_reporting_period_leaves_it_unknown():
    """A title that names only the edition month must not invent a reporting period."""
    info = classify_title("monetary_policy_review", "Pul Siyasəti İcmalı – Avqust 2026")
    assert info.reporting_start is None and info.reporting_end is None
    assert info.period_basis is None


# ---------------------------------------------------------------------- policy

def test_decision_table_is_read_by_header_not_position():
    text = (FIXTURES / "cba_mpr_2026-08_decisions_table.txt").read_text(encoding="utf-8")
    rows = pol.parse_decision_table(_passages(text, kind="table", page=6))
    assert len(rows) >= 12
    by_date = {r.date.isoformat(): r for r in rows}
    july = by_date["2026-07-31"]
    assert (july.floor, july.rate, july.ceiling) == (5.5, 6.5, 7.5)
    cut = by_date["2026-02-04"]
    assert cut.rate == 5.5 + 1.0 and cut.change_bp("rate") == -25.0    # 6.75% -> 6.50%
    assert july.change_bp("rate") == 0.0


def test_press_release_gives_announcement_rationale_and_announced_next_date():
    ex = extract_press_release(FIXTURES / "cba_decision_2026-07-31_en.html")
    passages = [{"kind": p.kind, "text": p.text, "page_index": 1, "printed_page": None,
                 "passage_id": f"pr#{p.ord}"} for p in ex.passages]
    rel = pol.parse_press_release(passages, "en", dt.date(2026, 7, 31))
    assert rel.action == "hold"
    assert rel.next_decision_date == dt.date(2026, 9, 23)
    assert rel.effective_date is None and "not stated" in rel.effective_basis
    assert "interest rate corridor" in rel.rationale


def test_unchanged_rate_with_changed_rationale_is_not_called_a_tightening():
    """The stance label describes the rate decision; a changed rationale is reported as such."""
    row = pol.DecisionRow(date=dt.date(2026, 7, 31), floor=5.5, rate=6.5, ceiling=7.5)
    prev = pol.DecisionRow(date=dt.date(2026, 6, 24), floor=5.5, rate=6.5, ceiling=7.5)
    same = pol.DecisionRelease(rationale="Inflation is within the target range and the corridor is unchanged.")
    different = pol.DecisionRelease(rationale="Upward revision to the inflation forecast supports a tighter stance, "
                                              "while excess foreign exchange supply supports accommodation.")
    unchanged = pol.stance_comparison(row, prev, same, same)
    changed = pol.stance_comparison(row, prev, different, same)
    assert unchanged["rate_change_bp"] == 0 and unchanged["label"] == "rate unchanged"
    assert changed["label"] == "rate unchanged, rationale changed"
    assert "tighten" not in changed["label"]


def test_forecast_horizon_is_dated_from_the_document_not_from_an_assumed_base():
    text = ("Mərkəzi Bankın iyul proqnozlarına əsasən illik inflyasiyanın 2026-cı ilin sonunda 6.1%, "
            "12 ay sonra (yəni 2027-ci ilin iyun ayında) 6%, 2027-ci ilin sonunda isə 5.8% olacağı proqnozlaşdırılır.")
    obs, _notes = pol.extract_forecasts(_passages(text), "az", publication_id="mpr:2026-08",
                                        vintage="2026-07", vintage_date=dt.date(2026, 7, 31))
    got = {(o.period_end.isoformat(), o.value) for o in obs}
    assert ("2026-12-31", 6.1) in got
    assert ("2027-12-31", 5.8) in got
    assert ("2027-06-30", 6.0) in got            # the document names June 2027, not vintage + 12 months
    assert all(o.period_type == "forecast" and o.scenario == "baseline" for o in obs)


def test_a_range_is_not_extracted_as_a_point_forecast():
    text = ("Proqnozlara görə ümumi ÜDM-in artım tempi 2026-cı il üçün 1-1.3% aralığında gözlənilə bilər.")
    obs, notes = pol.extract_forecasts(_passages(text), "az", publication_id="mpr:2026-08",
                                       vintage="2026-07", vintage_date=dt.date(2026, 7, 31))
    assert obs == []
    assert any("range" in n for n in notes)


def test_forecast_revisions_match_only_the_same_target_period():
    current = [{"series_id": "cba.forecast.inflation", "period_end": "2026-12-31", "value": 6.1,
                "scenario": "baseline", "forecast_vintage": "2026-07"},
               {"series_id": "cba.forecast.inflation", "period_end": "2027-12-31", "value": 5.8,
                "scenario": "baseline", "forecast_vintage": "2026-07"}]
    previous = [{"series_id": "cba.forecast.inflation", "period_end": "2026-12-31", "value": 5.6,
                 "scenario": "baseline", "forecast_vintage": "2026-05"}]
    rows = {r["target_period"]: r for r in pol.compare_forecasts(current, previous)}
    assert rows["2026-12-31"]["comparable"] and rows["2026-12-31"]["revision_pp"] == pytest.approx(0.5)
    assert rows["2027-12-31"]["comparable"] is False and rows["2027-12-31"]["revision_pp"] is None


# ------------------------------------------------------------------- stability

def test_stability_indicators_are_read_from_the_report_text():
    text = (FIXTURES / "cba_fsr_2025_excerpt.txt").read_text(encoding="utf-8")
    obs, _notes = stab.extract_indicators(_passages(text), "en", publication_id="fsr:2025-A",
                                          reporting_end=dt.date(2025, 12, 31), edition_label="2025 (annual)")
    values = {o.series_id: o.value for o in obs}
    assert values["cba.fsr.car"] == 17.6
    assert values["cba.fsr.lcr"] == 154.0
    assert values["cba.fsr.roa"] == 2.1 and values["cba.fsr.roe"] == 18.2   # "respectively" mapped in order
    assert all(o.period_end == dt.date(2025, 12, 31) for o in obs)
    car = next(o for o in obs if o.series_id == "cba.fsr.car")
    assert car.dims == {"basis": "regulatory"} and "not accounting equity" in car.basis


def test_a_regulatory_requirement_is_not_stored_as_an_outcome():
    text = ("Starting from June 2025, systemically important banks should maintain an LCR ratio of 100%, "
            "while other banks should maintain a ratio of 90%.")
    obs, _ = stab.extract_indicators(_passages(text), "en", publication_id="fsr:2024-A",
                                     reporting_end=dt.date(2024, 12, 31), edition_label="2024 (annual)")
    assert [o for o in obs if o.series_id == "cba.fsr.lcr"] == []


def test_a_segment_ratio_is_not_stored_as_the_sector_aggregate():
    text = "The NPL ratio of this segment increased by 0.2 pp to 2.7%."
    obs, notes = stab.extract_indicators(_passages(text), "en", publication_id="fsr:2025-A",
                                         reporting_end=dt.date(2025, 12, 31), edition_label="2025 (annual)")
    assert [o for o in obs if o.series_id == "cba.fsr.npl_ratio"] == []
    assert any("segment" in n for n in notes)


def test_stress_results_carry_scenario_and_are_not_forecasts():
    text = ("Under the baseline scenario, the capital adequacy ratio stands at 19.4% in 2026. Under the adverse "
            "scenario, the banking sector's CAR decreases by 2.6 pp in 2026 and by an additional 1.2 pp in 2027 to "
            "14.8% and 13.6%, respectively.")
    obs, meta = stab.extract_stress_tests(_passages(text), "en", publication_id="fsr:2025-A",
                                          reporting_end=dt.date(2025, 12, 31), edition_label="2025 (annual)")
    got = {(o.scenario, o.period_end.year): o.value for o in obs}
    assert got[("baseline", 2026)] == 19.4
    assert got[("adverse", 2026)] == 14.8 and got[("adverse", 2027)] == 13.6
    for o in obs:
        assert o.period_type == "stress_test_projection"
        assert "Not a forecast" in o.basis and o.dims["exercise"] == "2025-12-31"
    assert set(meta["scenarios"]) >= {"baseline", "adverse"}


def test_a_reading_inconsistent_with_other_editions_is_held_for_review(tmp_path):
    """One mis-read sentence in a back edition must not reach the fact pack."""
    from azmonitor.parsers.base import Observation

    db = Database(tmp_path / "t.sqlite")
    rows = [(dt.date(2023, 12, 31), 3.0), (dt.date(2024, 6, 30), 17.5), (dt.date(2024, 12, 31), 17.6),
            (dt.date(2025, 6, 30), 18.0), (dt.date(2025, 12, 31), 17.6)]
    for period, value in rows:
        db.store_observations("cba_financial_stability_report", f"doc:{period}", [Observation(
            series_id="cba.fsr.car", period_end=period, value=value, unit="%", dims={"basis": "regulatory"},
            period_type="period_end_ratio", value_raw="fixture sentence")])
    result = review_publication_series(db, write=True)
    flagged = {f["period_end"] for f in result["flagged"]}
    assert flagged == {"2023-12-31"}
    statuses = {r["period_end"]: r["validation_status"] for r in
                db.conn.execute("SELECT period_end, validation_status FROM observations WHERE status='current'")}
    assert statuses["2023-12-31"] == "needs_review" and statuses["2025-12-31"] == "verified"
    db.close()


# --------------------------------------------------- dates, languages, versions

def _seed_publication(db: Database, pub_id: str, pub_type: str, edition_key: str, reporting_end: str | None,
                      published_at: str | None, languages=("az", "en")) -> None:
    db.upsert_publication({"publication_id": pub_id, "source_id": "CBA", "pub_type": pub_type,
                           "edition_key": edition_key, "edition_label": edition_key,
                           "reporting_period_end": reporting_end, "published_at": published_at,
                           "published_at_basis": "test", "status": "extracted"})
    for i, lang in enumerate(languages, start=1):
        doc_id = f"{pub_id}:{lang}"
        db.upsert_document({"doc_id": doc_id, "source_id": "CBA", "dataset_id": pub_type, "document_url": f"http://x/{doc_id}",
                            "sha256": f"sha{i}", "retrieved_at": "2026-09-01T00:00:00+00:00",
                            "first_seen_at": "2026-09-01T00:00:00+00:00", "status": "parsed"})
        db.link_publication_document(doc_id, pub_id, lang, version=1)


def test_two_language_editions_are_one_publication(tmp_path):
    db = Database(tmp_path / "t.sqlite")
    _seed_publication(db, "monetary_policy_review:2026-08", "monetary_policy_review", "2026-08", None, "2026-08-26")
    pubs = db.publications(pub_type="monetary_policy_review")
    assert len(pubs) == 1
    docs = db.publication_documents("monetary_policy_review:2026-08")
    assert sorted(d["language"] for d in docs) == ["az", "en"]
    db.close()


def test_a_revised_file_at_the_same_url_keeps_the_earlier_version(tmp_path):
    db = Database(tmp_path / "t.sqlite")
    url = "http://uploads.example/fsr.pdf"
    for i, sha in enumerate(("aaa", "bbb"), start=1):
        db.upsert_document({"doc_id": f"fsr:{sha}", "source_id": "CBA", "dataset_id": "cba_financial_stability_report",
                            "document_url": url, "sha256": sha, "retrieved_at": f"2026-0{i}-01T00:00:00+00:00",
                            "first_seen_at": f"2026-0{i}-01T00:00:00+00:00", "status": "parsed", "version": i})
    versions = db.document_versions(url)
    assert [v["sha256"] for v in versions] == ["aaa", "bbb"]
    db.close()


def test_publication_date_is_never_the_reporting_period_or_the_download_time(tmp_path):
    db = Database(tmp_path / "t.sqlite")
    _seed_publication(db, "financial_stability_report:2025-A", "financial_stability_report", "2025-A",
                      "2025-12-31", "2026-04-13")
    pf = PublicationFacts(db, dt.date(2026, 9, 19))
    view = pf._publication_view(pf.latest("financial_stability_report"))
    assert view["reporting_period_end"] == "2025-12-31"
    assert view["published_at"] == "2026-04-13"
    assert view["reporting_period_end"] != view["published_at"]
    db.close()


def test_an_information_cutoff_excludes_publications_released_after_it(tmp_path):
    db = Database(tmp_path / "t.sqlite")
    _seed_publication(db, "policy_decision:1", "policy_decision", "2026-06-24", "2026-06-24", "2026-06-24")
    _seed_publication(db, "policy_decision:2", "policy_decision", "2026-07-31", "2026-07-31", "2026-07-31")
    visible = {p["publication_id"] for p in db.publications(pub_type="policy_decision", cutoff="2026-07-01")}
    assert visible == {"policy_decision:1"}
    db.close()


def test_older_stability_readings_keep_their_own_dates_and_are_not_carried_forward(tmp_path):
    from azmonitor.parsers.base import Observation

    db = Database(tmp_path / "t.sqlite")
    _seed_publication(db, "financial_stability_report:2025-A", "financial_stability_report", "2025-A",
                      "2025-12-31", "2026-04-13")
    db.store_observations("cba_financial_stability_report", "financial_stability_report:2025-A:en", [Observation(
        series_id="cba.fsr.car", period_end=dt.date(2025, 12, 31), value=17.6, unit="%", freq="A",
        period_type="period_end_ratio", dims={"basis": "regulatory"},
        publication_id="financial_stability_report:2025-A")])
    pf = PublicationFacts(db, dt.date(2026, 9, 19))
    dashboard = pf.stability_block()["dashboard"]
    row = next(r for r in dashboard if r["series_id"] == "cba.fsr.car")
    assert row["observation_date"] == "2025-12-31"
    assert row["publication"]["published_at"] == "2026-04-13"
    assert row["frequency"] == "A"
    # nothing was created for the banking month of the edition
    periods = {r["period_end"] for r in db.conn.execute(
        "SELECT period_end FROM observations WHERE series_id='cba.fsr.car'")}
    assert periods == {"2025-12-31"}
    db.close()


def test_an_indicator_the_monthly_tables_lack_is_not_still_called_unavailable(tmp_path):
    """The stability report publishes CAR and LCR, so the availability matrix must stop saying
    they are unverified and say instead what is held and at what frequency."""
    from azmonitor.parsers.base import Observation
    from azmonitor.facts import FactPackBuilder

    db = Database(tmp_path / "t.sqlite")
    _seed_publication(db, "financial_stability_report:2025-A", "financial_stability_report", "2025-A",
                      "2025-12-31", "2026-04-13")
    db.store_observations("cba_financial_stability_report", "financial_stability_report:2025-A:en", [
        Observation(series_id="cba.fsr.car", period_end=dt.date(2025, 12, 31), value=17.6, unit="%", freq="A",
                    period_type="period_end_ratio", dims={"basis": "regulatory"},
                    publication_id="financial_stability_report:2025-A"),
        Observation(series_id="cba.fsr.stress.car", period_end=dt.date(2027, 12, 31), value=13.6, unit="%", freq="A",
                    period_type="stress_test_projection", dims={"scenario": "adverse", "exercise": "2025-12-31"},
                    publication_id="financial_stability_report:2025-A"),
    ])
    av = FactPackBuilder(db, dt.date(2026, 9, 19)).availability_matrix()

    car = next(u for u in av["unverified"] if u["item"].startswith("Regulatory capital adequacy"))
    assert car["status"] == "available at a lower frequency"
    assert car["latest_period"] == "2025-12-31" and car["value"] == 17.6

    # and the slides that report it resolve, although the scenario and exercise dimensions of a
    # stress path are not addressable without knowing the edition
    assert [i for i in av["slides"]["M21"]["inputs"] if i["input"] == "cba.fsr.car"][0]["status"] == "available"
    assert [i for i in av["slides"]["M22"]["inputs"] if i["input"] == "cba.fsr.stress.car"][0]["status"] == "available"

    # an indicator nothing publishes is still reported as unverified, with the reason
    nsfr = next(u for u in av["unverified"] if u["item"].startswith("NSFR"))
    assert nsfr["status"] == "unverified" and nsfr["note"]
    db.close()


def test_the_rationale_comparison_shows_what_differs_not_the_shared_formula():
    """Two decision statements open with the same sentence. Showing the opening of each would put
    identical text under a heading that says the reasoning changed."""
    from azmonitor.facts import _rationale_difference

    shared = "The Management Board of the Central Bank decided to keep the interest rate corridor unchanged."
    prev = shared + " The decision reflects heightened geopolitical tensions and global financial conditions."
    cur = shared + " The decision reflects the upward revision to the inflation forecast."

    a, b = _rationale_difference(prev, cur)
    assert "Management Board" not in a and "Management Board" not in b
    assert "geopolitical" in a and "inflation forecast" in b

    # identical statements are reported as identical rather than as a difference
    same_a, same_b = _rationale_difference(prev, prev)
    assert same_a == same_b == "same wording as the other statement"

    # and a statement that only adds a sentence says so on the side that has nothing of its own
    only_a, only_b = _rationale_difference(shared, cur)
    assert only_a == "nothing the current statement does not also say"
    assert "inflation forecast" in only_b


def test_a_stability_brief_charts_one_exercise_not_three():
    """The 2023 round projected 2024 from end-2023 data and the 2025 round projects 2026 from
    end-2025 data. Putting them on one axis would draw a path nobody published."""
    from azmonitor.render.briefs import FsrBrief

    stab = {"stress_tests": {"results": [
        {"scenario": "baseline", "observation_date": "2024-12-31", "value": 18.1,
         "dims": {"exercise": "2023-12-31"}, "publication": {"id": "financial_stability_report:2023-A"}},
        {"scenario": "baseline", "observation_date": "2025-12-31", "value": 20.3,
         "dims": {"exercise": "2024-12-31"}, "publication": {"id": "financial_stability_report:2024-A"}},
        {"scenario": "baseline", "observation_date": "2026-12-31", "value": 19.4,
         "dims": {"exercise": "2025-12-31"}, "publication": {"id": "financial_stability_report:2025-A"}},
        {"scenario": "adverse", "observation_date": "2026-12-31", "value": 14.8,
         "dims": {"exercise": "2025-12-31"}, "publication": {"id": "financial_stability_report:2025-A"}},
    ]}}

    brief = FsrBrief.__new__(FsrBrief)
    brief.pub = {"publication_id": "financial_stability_report:2025-A"}
    kept = brief._this_exercise(stab)
    assert {r["value"] for r in kept} == {19.4, 14.8}
    assert {(r["dims"]["exercise"]) for r in kept} == {"2025-12-31"}

    # where the rows do not name this publication, the newest exercise is used rather than all of them
    brief.pub = {"publication_id": "financial_stability_report:2099-A"}
    assert {r["value"] for r in brief._this_exercise(stab)} == {19.4, 14.8}


def test_a_decision_dated_series_compares_with_the_previous_decision(tmp_path):
    """Meetings are irregular, so a twelve-month lag lands on nothing and the rate is reported as
    having no comparable prior value - when the comparison a reader wants is the meeting before."""
    from azmonitor.parsers.base import Observation
    from azmonitor.facts import FactPackBuilder

    db = Database(tmp_path / "t.sqlite")
    _seed_publication(db, "monetary_policy_review:2026-08", "monetary_policy_review", "2026-08",
                      "2026-07-31", "2026-08-26")
    db.store_observations("cba_monetary_policy_review", "monetary_policy_review:2026-08:az", [
        Observation(series_id="cba.policy.rate", period_end=dt.date(2026, 6, 24), value=6.5, unit="%", freq="D",
                    period_type="policy_rate_effective", publication_id="monetary_policy_review:2026-08"),
        Observation(series_id="cba.policy.rate", period_end=dt.date(2026, 7, 31), value=6.5, unit="%", freq="D",
                    period_type="policy_rate_effective", publication_id="monetary_policy_review:2026-08"),
    ])
    snap = FactPackBuilder(db, dt.date(2026, 9, 19)).snap("cba.policy.rate")

    assert snap["compare"] == "previous_observation"
    assert snap["prior"]["period"] == "2026-06-24"
    assert snap["change"] == 0.0        # unchanged is a finding; "no comparable value" is not
    db.close()


def test_release_translation_and_download_dates_are_three_separate_things(tmp_path):
    """A backfill downloads years of archive at once and an English edition appears weeks after the
    Azerbaijani one. Neither is a release, and neither may overwrite the release date."""
    db = Database(tmp_path / "t.sqlite")
    db.upsert_publication({"publication_id": "financial_stability_report:2025-A", "source_id": "CBA_STABILITY",
                           "pub_type": "financial_stability_report", "edition_key": "2025-A",
                           "original_language": "az", "published_at": "2026-04-13",
                           "published_at_basis": "HTTP Last-Modified header of the file"})
    # the translated edition arrives sixteen days later
    db.upsert_publication({"publication_id": "financial_stability_report:2025-A",
                           "translation_available_at": "2026-04-29",
                           "translation_available_basis": "HTTP Last-Modified header of the translated file"})
    row = db.get_publication("financial_stability_report:2025-A")

    assert row["published_at"] == "2026-04-13"           # the release, unmoved by the translation
    assert row["translation_available_at"] == "2026-04-29"
    assert row["first_seen_at"][:4] >= "2026"            # our download, later than both
    assert row["published_at"] < row["translation_available_at"] <= row["first_seen_at"]
    db.close()


def test_a_backfilled_publication_is_not_briefed_as_a_release(tmp_path):
    """Recency is judged on the release date, so loading a 2021 edition today briefs nothing."""
    from azmonitor.scheduling.run_due import _publication_briefs

    db = Database(tmp_path / "t.sqlite")
    db.upsert_publication({"publication_id": "financial_stability_report:2021-A", "source_id": "CBA_STABILITY",
                           "pub_type": "financial_stability_report", "edition_key": "2021-A",
                           "original_language": "az", "published_at": "2022-09-16"})
    state: dict = {}
    out = _publication_briefs(db, {"schedule": {"brief_within_days": 45}}, state, dry_run=True)

    assert out["generated"] == []
    assert any("outside the 45-day briefing window" in s["reason"] for s in out["skipped_old"])
    db.close()
