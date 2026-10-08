"""Deterministic facts-only narrative: complete, clearly labelled descriptive text from the fact pack."""
from __future__ import annotations

from typing import Any

from . import claims as C
from .contract import block, empty_narrative
from .fmt import change_word, money, num, plabel
from .titles import finding_titles


def _claims(s: dict[str, Any] | None, *which: str) -> list[str]:
    """Claim ids for the parts of a fact-pack entry a sentence actually uses."""
    return [c for c in (C.claim_for(s, w) for w in which) if c]


def _s(fp: dict[str, Any], ref: str) -> dict[str, Any] | None:
    return fp.get("metrics", {}).get(ref)


def _val(s: dict[str, Any] | None) -> float | None:
    return s["latest"]["value"] if s and s.get("latest") else None


def _per(s: dict[str, Any] | None) -> str:
    return plabel(s["latest"]["period"], s.get("period_type")) if s and s.get("latest") else "n/a"


def _prior(s: dict[str, Any] | None) -> float | None:
    return s["prior"]["value"] if s and s.get("prior") else None


def _unit_of(s: dict[str, Any] | None) -> str:
    """The unit the fact pack publishes, normalised for display: a contribution is in pp, not %."""
    u = (s or {}).get("unit") or "%"
    if u.startswith("%"):
        return "%"
    return u


def stmt_growth(fp, ref: str, what: str, unit_note: str = "y/y") -> str | None:
    s = _s(fp, ref)
    if not s or not s.get("latest"):
        return None
    v, p = _val(s), _prior(s)
    unit = _unit_of(s)
    txt = f"{what} was {num(v, 1, unit)} {unit_note} in {_per(s)}".replace("  ", " ").rstrip()
    used = ["latest"]
    if p is not None:
        txt += f" ({num(p, 1, unit)} in {plabel(s['prior']['period'], s.get('period_type'))})"
        used.append("prior")
    return block(txt + ".", _claims(s, *used))


def stmt_level(fp, ref: str, what: str) -> str | None:
    s = _s(fp, ref)
    if not s or not s.get("latest"):
        return None
    v = _val(s)
    txt = f"{what} stood at {money(v, s.get('unit') or 'AZN mln')} at {_per(s)}"
    used = ["latest"]
    if s.get("change") is not None:
        ch = s["change"]
        unit = s.get("unit") or "AZN mln"
        txt += f", {change_word(ch, 'up', 'down')} {money(abs(ch), unit) if unit in ('AZN mln', 'USD mln') else num(abs(ch), 1)} on {plabel(s['prior']['period'], s.get('period_type'))}"
        used.append("change")
    return block(txt + ".", _claims(s, *used))


def stmt_share(fp, ref: str, what: str) -> str | None:
    s = _s(fp, ref)
    if not s or not s.get("latest"):
        return None
    txt = f"{what} was {num(_val(s), 1, _unit_of(s))} at {_per(s)}"
    used = ["latest"]
    if s.get("change") is not None:
        txt += f", {num(s['change'], 1, 'pp', sign=True)} versus {plabel(s['prior']['period'], s.get('period_type'))}"
        used.append("change")
    return block(txt + ".", _claims(s, *used))


def stmt_range(fp, sid: str, key: str, ref: str, what: str, unit: str = "%", dims: dict[str, Any] | None = None) -> str | None:
    """'Over the period shown, <what> ranged between 2.3% (end-Oct 2025) and 20.0% (end-Oct 2024).'

    The low and the high are points of the series the slide plots, so each is a claim the
    validator can resolve."""
    rng = ((fp.get("slides") or {}).get(sid) or {}).get(key) or {}
    lo, hi = rng.get("min"), rng.get("max")
    s = _s(fp, ref)
    if not (lo and hi and s) or lo["period"] == hi["period"]:
        return None
    ptype = s.get("period_type")
    fmt = (lambda v: num(v, 1, unit)) if unit in ("%", "pp") else (lambda v: num(v, 1))
    txt = (f"Over the period shown, {what} ranged between {fmt(lo['value'])} ({plabel(lo['period'], ptype)}) "
           f"and {fmt(hi['value'])} ({plabel(hi['period'], ptype)}).")
    claims = [C.format_claim_id(s["id"], p["period"], "level", dims if dims is not None else (s.get("dims") or {})) for p in (lo, hi)]
    return block(txt, claims)


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
         stmt_growth(fp, "cba.loans.sector.households.contrib", "The household contribution to real-sector loan growth", "")],
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
    put("M23", "Real credit growth and credit-to-GDP",
        [stmt_growth(fp, "cba.loans.total_ci.real.yoy", "Real loan growth (deflated by CPI)", "y/y"),
         stmt_range(fp, "M23", "real_range", "cba.loans.total_ci.real.yoy", "real loan growth"),
         stmt_share(fp, "cba.credit_to_gdp", "Loans to the economy as a share of trailing four-quarter GDP")],
        caveat="Real growth deflates the loan stock by consumer prices; the FX part of the stock also moves with the exchange rate.")
    put("M24", "New lending against the loan stock",
        [stmt_growth(fp, "cba.new_loans.total.3m.yoy", "Growth of new loans over three months", "y/y"),
         stmt_level(fp, "cba.new_loans.total.3m", "New loans over the last three months"),
         stmt_range(fp, "M24", "fx_range", "cba.new_loans.fx_share", "the FX share of new loans")],
        caveat="New loans are gross originations including refinancing, so they overstate net credit creation.")
    put("M25", "Provision coverage of non-performing loans",
        [stmt_share(fp, "cba.bank.allowance_to_npl", "Loan-loss allowance / NPL"),
         stmt_range(fp, "M25", "coverage_range", "cba.bank.allowance_to_npl", "coverage"),
         stmt_growth(fp, "cba.bank.pnl.provisions.yoy", "Growth of provision charges (YTD)", "y/y")],
        caveat="The allowance is a balance-sheet stock and provision charges a P&L flow; write-offs reduce both the allowance and NPLs.")
    put("M26", "What drives deposit growth",
        [stmt_growth(fp, "cba.deposits.hh.contrib", "The household contribution to deposit growth", ""),
         stmt_growth(fp, "cba.deposits.nfc.contrib", "The non-financial corporate contribution", ""),
         stmt_range(fp, "M26", "nfc_range", "cba.deposits.nfc.contrib", "the non-financial corporate contribution", unit="pp")],
        caveat="Contributions sum to total growth; aggregate figures do not show concentration in particular depositors.")
    put("M27", "Dollarisation on both sides of the balance sheet",
        [stmt_share(fp, "cba.loans.fx_share", "The FX share of loans"), stmt_share(fp, "cba.deposits.fx_share", "The FX share of deposits"),
         stmt_share(fp, "cba.fx_share.deposits_minus_loans", "The gap between the deposit and loan FX shares")],
        caveat="Shares at current exchange rates; they show neither an open FX position nor whether FX borrowers are hedged.")
    put("M28", "Policy rate, bank pricing and money growth",
        [stmt_share(fp, "cba.policy.rate.month_end", "The refinancing rate in force"),
         stmt_share(fp, "cba.rates.new.deposit|{\"currency\": \"AZN\"}", "The average rate on new AZN term deposits"),
         stmt_growth(fp, "cba.money.m2.yoy", "M2 growth", "y/y")],
        caveat="New-business rates are monthly averages across maturities and institutions; composition shifts move them too.")
    put("M29", "What moved net profit",
        [stmt_level(fp, "cba.bank.pnl.net_interest_income", "Net interest income (YTD)"),
         stmt_level(fp, "cba.bank.pnl.provisions", "Provision charges (YTD)"),
         stmt_share(fp, "cba.bank.pnl.effective_tax_rate", "Profit tax as a share of pre-tax profit")],
        caveat="Prudential P&L, not IFRS; YTD flows compared with the same months a year earlier.")
    # the title states the finding when the fact pack has what it needs; the topic title stays otherwise
    for sid, title in finding_titles(fp).items():
        if sid in slides:
            slides[sid]["title"] = title
        else:                       # a slide whose text is its own (policy): only the title is written here
            slides[sid] = {"title": title, "interpretations": [], "so_what": "", "caveat": ""}
    nar["slides"] = slides
    # findings: five largest documented moves from the scorecard
    rows = fp["slides"]["M03"]["rows"]
    cands = []
    for r in rows:
        if r.get("latest") and r.get("change") is not None:
            cands.append(r)
    cands.sort(key=lambda r: -abs(r["change"]))
    for i, r in enumerate(cands[:5], start=1):
        statement = (f"{r['row_label']}: {num(r['latest']['value'], 1, r.get('unit'))} in {plabel(r['latest']['period'], r.get('period_type'))}, "
                     f"{num(r['change'], 1, 'pp' if r.get('kind') == 'pp' else r.get('unit'), sign=True)} versus {plabel(r['prior']['period'], r.get('period_type'))}.")
        nar["findings"].append({"id": f"F{i}", "rank": i, "slide_id": _slide_for(r["key"]), "classification": "observed_fact",
                                "statement": block(statement, _claims(r, "latest", "change")),
                                "metric_refs": [r.get("id") or ("scorecard." + r["key"])], "period": r["latest"]["period"], "comparison": r.get("compare"),
                                "banking_relevance": "Descriptive edition: relevance to be assessed by the reader.", "caveat": "", "direction": "neutral", "status": "new"})
    nar["questions"] = [
        {"question": "Which published movements in this edition warrant a source check against internal data?", "signal": "Largest scorecard changes", "why": "Facts-only edition lists movements without interpretation",
         "internal_data": "Portfolio and funding data by segment", "watch": "Next CBA monetary release", "function": "Credit Risk / Treasury"},
    ]
    return nar


def _slide_for(key: str) -> str:
    return {"non_oil_growth": "M04", "gdp_growth": "M04", "cpi_yoy": "M05", "real_wage": "M05", "loans_yoy": "M08", "deposits_yoy": "M11", "deposit_fx_share": "M12",
            "npl_ratio": "M10", "overdue_ratio": "M10", "spread_azn": "M13", "net_profit": "M15", "equity_assets": "M16", "liquid_assets": "M16"}.get(key, "M03")
