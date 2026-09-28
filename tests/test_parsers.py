import datetime as dt

from azmonitor import config
from azmonitor.parsers import cba_bank_overview, cba_matrix, cba_rates
from azmonitor.parsers.ssc_html import parse_html_text
from azmonitor.parsers.ssc_report import parse_text


def _spec(dataset_id):
    return config.dataset(dataset_id)[2]["parse"]


def test_cba_month_matrix_extracts_month_end_stocks(fixtures):
    res = cba_matrix.parse(fixtures / "cba_loans_by_institution_2_6.xlsx", _spec("cba_loans_by_institution"), source_id="CBA_MONETARY", dataset_id="cba_loans_by_institution")
    assert res.ok
    tot = {o.period_end: o for o in res.observations if o.series_id == "cba.loans.total_ci"}
    assert dt.date(2026, 7, 31) in tot and abs(tot[dt.date(2026, 7, 31)].value - 34155.08) < 0.01
    assert tot[dt.date(2026, 7, 31)].cell_ref.startswith("2.6!")
    assert all(o.period_type == "month_end_stock" for o in res.observations)
    # year header rows are not stored as observations
    assert not any(o.period_end.month == 12 and o.period_end.day != 31 for o in res.observations)


def test_cba_components_reconcile_to_total(fixtures):
    from azmonitor.pipeline import run_dataset_checks

    ds = config.dataset("cba_loans_by_institution")[2]
    res = cba_matrix.parse(fixtures / "cba_loans_by_institution_2_6.xlsx", ds["parse"], source_id="CBA_MONETARY", dataset_id=ds["id"])
    checks = run_dataset_checks(ds, res.observations)
    recent = [c for c in checks if c["period_end"] >= "2020-01-31"]
    assert recent and all(c["ok"] for c in recent)


def test_cba_rates_date_convention(fixtures):
    res = cba_rates.parse(fixtures / "cba_rates_new_3_2_1.xlsx", _spec("cba_rates_new"), source_id="CBA_MONETARY", dataset_id="cba_rates_new")
    assert res.ok
    azn = {o.period_end: o.value for o in res.observations if o.series_id == "cba.rates.new.loan" and o.dims == {"currency": "AZN"}}
    # block dated 2026-08-01 refers to July 2026
    assert dt.date(2026, 7, 31) in azn and abs(azn[dt.date(2026, 7, 31)] - 18.96) < 1e-9
    assert dt.date(2026, 8, 1) not in azn


def test_cba_bank_overview_npl_labels_and_scaling(fixtures):
    res = cba_bank_overview.parse(fixtures / "cba_bank_npl_5_6.xlsx", _spec("cba_bank_npl"), source_id="CBA_BANKING", dataset_id="cba_bank_npl")
    assert res.ok
    ratio = [o for o in res.observations if o.series_id == "cba.bank.npl.ratio" and o.period_end == dt.date(2026, 7, 31)][0]
    assert 0 < ratio.value < 10 and ratio.unit == "%"   # published as a fraction, scaled x100
    assert "Qeyri-işlək" in [o.label_original for o in res.observations if o.series_id == "cba.bank.npl.total"][0]


def test_ssc_html_current_layout(fixtures):
    res = parse_html_text((fixtures / "ssc_macro_page_2026-08_current_layout.html").read_text(encoding="utf-8"), source_id="SSC_MACRO")
    assert res.ok and res.meta["layout"] == "current"
    obs = {(o.series_id, o.period_end): o for o in res.observations}
    assert obs[("ssc.hl.gdp.level", dt.date(2026, 8, 31))].value == 87709.5
    assert obs[("ssc.hl.gdp_nonoil.growth", dt.date(2026, 8, 31))].value == 2.1
    # row-specific cutoffs override the headline period
    assert obs[("ssc.hl.credit.level", dt.date(2026, 7, 31))].value == 34155.1
    assert obs[("ssc.hl.wage.level", dt.date(2026, 7, 31))].period_type == "ytd_average"
    assert obs[("ssc.hl.exports.level", dt.date(2026, 7, 31))].value == 20136.7
    assert obs[("ssc.hl.strategic_reserves.level", dt.date(2026, 8, 31))].period_type == "month_end_stock"


def test_ssc_html_legacy_layout_index_to_growth(fixtures):
    res = parse_html_text((fixtures / "ssc_macro_page_2025-09_legacy_layout.html").read_text(encoding="utf-8"), source_id="SSC_MACRO")
    assert res.ok and res.meta["layout"] == "legacy"
    obs = {(o.series_id, o.period_end): o for o in res.observations}
    assert abs(obs[("ssc.hl.gdp.growth", dt.date(2025, 9, 30))].value - 1.3) < 1e-9
    assert obs[("ssc.hl.credit.level", dt.date(2025, 8, 31))].value == 30464.6      # "01 sentyabr vəziyyətinə"
    assert obs[("ssc.hl.wage.level", dt.date(2025, 8, 31))].value == 1093.8         # "*" marker = Jan-Aug window
    assert obs[("ssc.hl.cpi_yoy_month.growth", dt.date(2025, 9, 30))].period_type == "monthly_growth_yoy"


def test_ssc_pdf_report_tables(fixtures):
    res = parse_text((fixtures / "ssc_report_2026-08_excerpt.txt").read_text(encoding="utf-8"), source_id="SSC_MACRO")
    assert not res.errors
    obs = {(o.series_id, o.period_end): o.value for o in res.observations}
    assert obs[("ssc.hl.gdp.level", dt.date(2026, 8, 31))] == 87709.5
    assert obs[("ssc.gdp.sector.construction.real_index_ytd", dt.date(2026, 8, 31))] == 87.5
    assert obs[("ssc.gdp.non_oil.nominal_ytd", dt.date(2026, 8, 31))] == 61527.6
    assert obs[("ssc.cpi.all.mom_index", dt.date(2026, 8, 31))] == 100.2
    assert obs[("ssc.hl.hh_deposits.azn.level", dt.date(2026, 7, 31))] == 12875.8
