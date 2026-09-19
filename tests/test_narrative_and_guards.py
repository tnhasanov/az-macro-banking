import json
from pathlib import Path

from azmonitor.narrative import facts_only
from azmonitor.narrative.validate import apply_fallback, validate_narrative


def _fp():
    return {
        "report_type": "monthly", "as_of": "2026-09-19", "lang": "en", "fact_pack_hash": "abc",
        "edition": {"banking_period": "2026-07-31", "macro_period": "2026-08-31", "cpi_period": "2026-08-31", "edition_month": "2026-07"},
        "metrics": {"cba.loans.total_ci.yoy": {"id": "cba.loans.total_ci.yoy", "label": "Loans y/y", "unit": "%", "period_type": "month_end_stock_growth_yoy", "compare": "lag1",
                                              "latest": {"period": "2026-07-31", "value": 12.880006}, "prior": {"period": "2026-06-30", "value": 12.0709}, "change": 0.809}},
        "approved_numbers": [{"ref": "cba.loans.total_ci.yoy", "value": 12.880006, "period": "2026-07-31", "unit": "%"},
                             {"ref": "cba.loans.total_ci.yoy.prior", "value": 12.0709, "period": "2026-06-30", "unit": "%"},
                             {"ref": "cba.loans.total_ci", "value": 34155.08, "period": "2026-07-31", "unit": "AZN mln"},
                             {"ref": "scorecard.loans_yoy", "value": 12.880006, "period": "2026-07-31", "unit": "%"},
                             {"ref": "scorecard.loans_yoy.change", "value": 0.809, "period": "2026-07-31", "unit": "pp"}],
        "slides": {"M03": {"rows": [{"key": "loans_yoy", "row_label": "Loans y/y", "id": "cba.loans.total_ci.yoy", "unit": "%", "kind": "pp", "compare": "lag1", "period_type": "x",
                                      "latest": {"period": "2026-07-31", "value": 12.880006}, "prior": {"period": "2026-06-30", "value": 12.0709}, "change": 0.809}]}},
    }


def test_validator_accepts_rounded_fact_pack_numbers_and_rejects_others():
    fp = _fp()
    good = {"fact_pack_hash": "abc", "findings": [{"id": "F1", "slide_id": "M08", "classification": "observed_fact", "statement": "Loans grew 12.9% y/y (12.1% in June) to AZN 34.2 bn.",
                                                    "metric_refs": ["cba.loans.total_ci.yoy"], "period": "2026-07-31"}],
            "slides": {"M08": {"title": "Lending up 12.9%", "interpretations": ["Loans rose to AZN 34,155 mln."], "so_what": "x"}}, "questions": []}
    v = validate_narrative(good, fp)
    assert v["ok"], v["problems"]
    bad = json.loads(json.dumps(good))
    bad["findings"][0]["statement"] = "Loans grew 14.2% y/y."           # unsupported number
    bad["findings"].append({"id": "F2", "slide_id": "M99", "classification": "forecast", "statement": "Defaults will hit 5% next year.", "metric_refs": ["made.up"], "period": "2027-01-31"})
    bad["slides"]["M08"]["interpretations"] = ["Loans rose 9.9% because of oil."]
    v = validate_narrative(bad, fp)
    assert not v["ok"]
    assert set(v["rejected_findings"]) == {"F1", "F2"} and v["rejected_slides"] == ["M08"]
    issues = " ".join(p["issue"] for p in v["problems"])
    assert "unknown slide" in issues and "bad classification" in issues and "unknown metric ref" in issues and "number not in approved" in issues


def test_fallback_replaces_rejected_text_with_facts_only():
    fp = _fp()
    fallback = facts_only.generate(fp)
    nar = {"fact_pack_hash": "zzz", "findings": [{"id": "F1", "slide_id": "M08", "classification": "hypothesis", "statement": "Loans grew 77% y/y.", "metric_refs": [], "period": "2026-07-31"}],
           "slides": {"M08": {"title": "Loans up 77%", "interpretations": [], "so_what": ""}}, "questions": []}
    v = validate_narrative(nar, fp)
    out = apply_fallback(nar, fallback, v)
    assert out["slides"]["M08"].get("fallback") is True
    assert out["findings"] == fallback["findings"] and out.get("stale_fact_pack") is True


def test_facts_only_narrative_is_grounded():
    fp = _fp()
    nar = facts_only.generate(fp)
    v = validate_narrative(nar, fp)
    assert v["ok"], v["problems"]
    assert nar["mode"] == "facts_only" and nar["findings"][0]["classification"] == "observed_fact"


def test_failed_parse_does_not_touch_stored_observations(tmp_path, monkeypatch):
    """A parser failure marks the document parse_failed and leaves prior observations and state intact."""
    from azmonitor.discovery import DiscoveredDocument
    from azmonitor.ingest.fetch import Fetched
    from azmonitor.pipeline import Pipeline
    from azmonitor import config

    monkeypatch.setenv("AZMONITOR_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("AZMONITOR_OUTPUT_DIR", str(tmp_path / "out"))
    p = Pipeline(offline=True)
    sid, scfg, ds = config.dataset("cba_loans_by_institution")
    p.db.store_observations(ds["id"], "cba_loans_by_institution:prior", [])
    from azmonitor.parsers.base import Observation
    import datetime as dt

    p.db.store_observations(ds["id"], "cba_loans_by_institution:prior", [Observation(series_id="cba.loans.total_ci", period_end=dt.date(2026, 6, 30), value=33881.09)])
    p.db.set_dataset_state(ds["id"], status="parsed", latest_period_end="2026-06-30")
    garbage = b"not a workbook"
    monkeypatch.setattr(p.fetcher, "get", lambda url, **kw: Fetched(url=url, final_url=url, content=garbage, status=200, content_type="application/octet-stream", last_modified=None, etag=None,
                                                                 retrieved_at="2026-09-19T00:00:00+00:00", sha256="deadbeef" * 8))
    d = DiscoveredDocument(source_id=sid, dataset_id=ds["id"], discovery_url="x", document_url="https://example.invalid/file.xlsx", title="broken", extension="xlsx")
    stats = {}
    p.process_document(d, sid, ds, stats)
    assert stats[ds["id"]]["errors"]
    assert p.db.dataset_states()[ds["id"]]["status"] == "parse_failed"
    cur = p.db.current_observations(["cba.loans.total_ci"])
    assert len(cur) == 1 and cur[0]["value"] == 33881.09
