"""Deterministic facts-only narrative: complete, clearly labelled descriptive text from the fact pack."""
from __future__ import annotations

from typing import Any

from .contract import empty_narrative
from .fmt import change_word, money, num, plabel


def _s(fp: dict[str, Any], ref: str) -> dict[str, Any] | None:
    return fp.get("metrics", {}).get(ref)


def _val(s: dict[str, Any] | None) -> float | None:
    return s["latest"]["value"] if s and s.get("latest") else None


def _per(s: dict[str, Any] | None) -> str:
    return plabel(s["latest"]["period"], s.get("period_type")) if s and s.get("latest") else "n/a"


def _prior(s: dict[str, Any] | None) -> float | None:
    return s["prior"]["value"] if s and s.get("prior") else None


def stmt_growth(fp, ref: str, what: str, unit_note: str = "y/y") -> str | None:
    s = _s(fp, ref)
    if not s or not s.get("latest"):
        return None
    v, p = _val(s), _prior(s)
    txt = f"{what} was {num(v, 1, '%')} {unit_note} in {_per(s)}"
    if p is not None:
        txt += f" ({num(p, 1, '%')} in {plabel(s['prior']['period'], s.get('period_type'))})"
    return txt + "."


def stmt_level(fp, ref: str, what: str) -> str | None:
    s = _s(fp, ref)
    if not s or not s.get("latest"):
        return None
    v = _val(s)
    txt = f"{what} stood at {money(v, s.get('unit') or 'AZN mln')} at {_per(s)}"
    if s.get("change") is not None:
        ch = s["change"]
        unit = s.get("unit") or "AZN mln"
        txt += f", {change_word(ch, 'up', 'down')} {money(abs(ch), unit) if unit in ('AZN mln', 'USD mln') else num(abs(ch), 1)} on {plabel(s['prior']['period'], s.get('period_type'))}"
    return txt + "."


def stmt_share(fp, ref: str, what: str) -> str | None:
    s = _s(fp, ref)
    if not s or not s.get("latest"):
        return None
    txt = f"{what} was {num(_val(s), 1, '%')} at {_per(s)}"
    if s.get("change") is not None:
        txt += f", {num(s['change'], 1, 'pp', sign=True)} versus {plabel(s['prior']['period'], s.get('period_type'))}"
    return txt + "."


def generate(fp: dict[str, Any]) -> dict[str, Any]:
    nar = empty_narrative(fp, "facts_only")
    ed = fp["edition"]
    nar["cover"]["headline"] = "Facts-only descriptive edition: observed changes in official CBA and SSC statistics"
    slides: dict[str, Any] = {}

    def put(sid: str, title: str, items: list[str | None], so_what: str = "", caveat: str = ""):
        items = [i for i in items if i]
        slides[sid] = {"title": title, "interpretations": items[:3], "so_what": so_what or "Descriptive edition: no interpretation is asserted beyond the observed values.",
                       "caveat": caveat}

    put("M04", "Economic growth: published real growth by sector",
        [stmt_growth(fp, "ssc.hl.gdp.growth", "Real GDP growth (YTD)", "vs the same period of the previous year"),
         stmt_growth(fp, "ssc.hl.gdp_nonoil.growth", "Non-oil-gas GDP real growth (YTD)", "vs the same period of the previous year"),
         stmt_growth(fp, "ssc.hl.gdp_oil.growth", "Oil-gas GDP real growth (YTD)", "vs the same period of the previous year")],
        caveat="YTD growth is cumulative; a change between editions is a change in the cumulative rate, not a monthly reading.")
    put("M05", "Inflation and income: CPI and nominal wages",
        [stmt_growth(fp, "ssc.cpi.all.yoy", "CPI inflation", "y/y"), stmt_growth(fp, "ssc.cpi.all.ytd", "Average CPI inflation (YTD)", "vs the previous year"),
         stmt_growth(fp, "ssc.hl.wage.growth", "Average nominal wage growth (YTD)", "y/y"), stmt_growth(fp, "ssc.real_wage_growth_est", "Estimated real wage growth (YTD)", "")],
        caveat="Real wage growth is an estimate: nominal YTD wage growth deflated by YTD average CPI; it is not a measure of loan affordability.")
    put("M06", "Sector activity: published growth by sector", [stmt_growth(fp, f"ssc.hl.{k}.growth", lbl, "YTD y/y") for k, lbl in [("industry_nonoil", "Non-oil-gas industry"), ("agriculture", "Agriculture"), ("transport", "Transport and storage services")]])
    put("M07", "External position: trade flows and reserves",
        [stmt_level(fp, "ssc.hl.exports.level", "Exports (YTD)"), stmt_level(fp, "ssc.hl.imports.level", "Imports (YTD)"), stmt_level(fp, "cba.reserves.official_usd", "CBA official foreign reserves")],
        caveat="Trade data lag the banking anchor; reserves and trade are shown separately and no causal link is asserted.")
    put("M08", "Lending: loan stock and growth",
        [stmt_level(fp, "cba.loans.total_ci", "Loans to the economy (all credit institutions)"), stmt_growth(fp, "cba.loans.total_ci.yoy", "Loan growth", "y/y"),
         stmt_growth(fp, "cba.loans.sector.households.contrib", "Household loans contributed", "pp to real-sector loan growth")],
        caveat="Stock changes combine originations, repayments, write-offs, reclassifications and valuation; none is inferred from the stock alone.")
    put("M09", "Sector credit versus sector activity", ["Nominal loan-stock growth (CBA, y/y) is plotted against real value-added growth (SSC, YTD) for mapped sectors; measures differ in coverage and basis."],
        caveat="A divergence is a prompt to ask sector specialists, not evidence of over-lending.")
    put("M10", "Asset quality: NPL balance and ratio",
        [stmt_level(fp, "cba.bank.npl.total", "Non-performing loans (banks, prudential)"), stmt_share(fp, "cba.bank.npl.ratio", "The published NPL ratio"),
         stmt_share(fp, "cba.loans.overdue_ratio", "Overdue loans as a share of all credit-institution loans")],
        caveat="Overdue loans and NPL are different published concepts; the ratio decomposition is arithmetic and does not establish cures or recoveries.")
    put("M11", "Deposits: total and by depositor",
        [stmt_level(fp, "cba.deposits.total", "Total deposits in credit institutions"), stmt_growth(fp, "cba.deposits.total.yoy", "Deposit growth", "y/y"),
         stmt_growth(fp, "cba.deposits.hh.total.yoy", "Household deposit growth", "y/y")])
    put("M12", "Funding mix: currency and term structure",
        [stmt_share(fp, "cba.deposits.fx_share", "The FX share of total deposits"), stmt_share(fp, "cba.deposits.hh.fx_share", "The FX share of household deposits"),
         stmt_share(fp, "cba.loans.fx_share", "The FX share of loans")], caveat="Shares are at current exchange rates and are not exchange-rate-adjusted flows.")
    put("M13", "Pricing: new-business rates and indicative spread",
        [stmt_share(fp, "cba.rates.new.loan|{\"currency\": \"AZN\"}", "The average rate on new AZN loans"),
         stmt_share(fp, "cba.rates.new.deposit|{\"currency\": \"AZN\"}", "The average rate on new AZN term deposits"),
         stmt_share(fp, "cba.rates.new.spread|{\"currency\": \"AZN\"}", "The indicative AZN pricing spread")],
        caveat="Aggregate rates change with product and customer mix even without like-for-like repricing; the spread is not NIM.")
    put("M14", "Credit growth versus funding growth", [stmt_growth(fp, "m14.loans_yoy", "Loan growth", "y/y"), stmt_growth(fp, "m14.deposits_yoy", "Deposit growth", "y/y"),
                                                       stmt_share(fp, "cba.ldr", "The loan-to-deposit ratio")], caveat="The loan-to-deposit ratio is not a regulatory liquidity ratio.")
    put("M15", "Profitability: banking-sector P&L (YTD)", [stmt_level(fp, "cba.bank.pnl.net_profit", "Net profit (YTD)"), stmt_growth(fp, "cba.bank.pnl.net_profit.yoy", "Net profit growth (YTD)", "y/y"),
                                                           stmt_share(fp, "cba.bank.pnl.cost_to_income", "Cost-to-income (YTD)")],
        caveat="ROA/ROE are annualised from YTD profit over average month-end balances; prudential P&L, not IFRS.")
    put("M16", "Capital and liquidity: book measures", [stmt_level(fp, "cba.bank.capital.total|{\"currency\": \"all\"}", "Total capital (book)"), stmt_share(fp, "cba.bank.equity_to_assets", "Capital to assets"),
                                                        stmt_share(fp, "cba.bank.liquid_assets_ratio", "Liquid assets to total assets")],
        caveat="Book ratios from prudential balance-sheet reporting; not regulatory capital adequacy, LCR or NSFR.")
    put("M17", "Regional lending and household savings", ["Loans (banks, by booking region) and household savings by economic region are shown as shares of the national total."],
        caveat="Booking location can differ from the location of economic activity; Baku includes head-office bookings.")
    nar["slides"] = slides
    # findings: five largest documented moves from the scorecard
    rows = fp["slides"]["M03"]["rows"]
    cands = []
    for r in rows:
        if r.get("latest") and r.get("change") is not None:
            cands.append(r)
    cands.sort(key=lambda r: -abs(r["change"]))
    for i, r in enumerate(cands[:5], start=1):
        nar["findings"].append({"id": f"F{i}", "rank": i, "slide_id": _slide_for(r["key"]), "classification": "observed_fact",
                                "statement": f"{r['row_label']}: {num(r['latest']['value'], 1, r.get('unit'))} in {plabel(r['latest']['period'], r.get('period_type'))}, "
                                             f"{num(r['change'], 1, 'pp' if r.get('kind') == 'pp' else r.get('unit'), sign=True)} versus {plabel(r['prior']['period'], r.get('period_type'))}.",
                                "metric_refs": ["scorecard." + r["key"]], "period": r["latest"]["period"], "comparison": r.get("compare"),
                                "banking_relevance": "Descriptive edition: relevance to be assessed by the reader.", "caveat": "", "direction": "neutral", "status": "new"})
    nar["questions"] = [
        {"question": "Which published movements in this edition warrant a source check against internal data?", "signal": "Largest scorecard changes", "why": "Facts-only edition lists movements without interpretation",
         "internal_data": "Portfolio and funding data by segment", "watch": "Next CBA monetary release", "function": "Credit Risk / Treasury"},
    ]
    return nar


def _slide_for(key: str) -> str:
    return {"non_oil_growth": "M04", "gdp_growth": "M04", "cpi_yoy": "M05", "real_wage": "M05", "loans_yoy": "M08", "deposits_yoy": "M11", "deposit_fx_share": "M12",
            "npl_ratio": "M10", "overdue_ratio": "M10", "spread_azn": "M13", "net_profit": "M15", "equity_assets": "M16", "liquid_assets": "M16"}.get(key, "M03")
