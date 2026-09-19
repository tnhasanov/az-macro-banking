"""Parser registry: parser id -> callable(path, spec, source_id=, dataset_id=, context=)."""
from __future__ import annotations

from . import cba_bank_overview, cba_matrix, cba_rates, cba_regions, ssc_html, ssc_report, ssc_xls

PARSERS = {
    "cba_month_matrix": cba_matrix.parse,
    "cba_rates_blocks": cba_rates.parse,
    "cba_bank_overview": cba_bank_overview.parse,
    "cba_region_snapshot": cba_regions.parse,
    "ssc_monthly_report_pdf": ssc_report.parse,
    "ssc_macro_html_table": ssc_html.parse,
    "ssc_price_bulletin_xls": ssc_xls.parse_price_bulletin,
    "ssc_cpi_index_xlsx": ssc_xls.parse_cpi_index,
    "ssc_gdp_quarterly_xls": ssc_xls.parse_gdp_quarterly,
    "ssc_gdp_oil_nonoil_annual_xls": ssc_xls.parse_gdp_oil_nonoil_annual,
    "ssc_wages_annual_xls": ssc_xls.parse_wages_annual,
}


def get_parser(name: str):
    if name in (None, "none"):
        return None
    try:
        return PARSERS[name]
    except KeyError:
        raise KeyError(f"unknown parser {name!r}; known: {sorted(PARSERS)}")
