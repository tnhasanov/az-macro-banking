"""Slide titles that state the finding, written from the fact pack.

A topic title ("Lending: loan stock and growth") tells the reader what the slide is about; a finding
title ("Loan growth rose 1.6 pp to 14.5% y/y at end-Aug 2026") tells them what happened. These are the
titles the facts-only edition carries, so a deck nobody has written commentary for still reads as
findings. They stay descriptive: what moved, by how much and when, never why.

Every number is bound to a claim, and the binding is chosen so the validator's direction check is
meaningful: a level (a growth rate, a ratio) is bound as a level, and only a movement is bound as a
change, so "rose 1.6 pp" is checked against the sign of the change and "to 14.5%" is not mistaken
for a movement. A title whose inputs are missing is simply not written; the slide keeps its topic
title.
"""
from __future__ import annotations

from typing import Any, Callable

from .claims import format_claim_id
from .contract import block
from .fmt import money, num, plabel

FULL_MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October",
               "November", "December"]
COMPARED = {"lag12": "a year earlier", "lag1": "a month earlier", "prior_edition": "the previous edition",
            "previous_observation": "the previous decision"}


def _e(fp: dict[str, Any], ref: str) -> dict[str, Any] | None:
    e = (fp.get("metrics") or {}).get(ref)
    return e if e and e.get("latest") and e["latest"].get("value") is not None else None


def _v(e: dict[str, Any]) -> float:
    return float(e["latest"]["value"])


def _c(e: dict[str, Any]) -> float | None:
    return None if e.get("change") is None else float(e["change"])


def _per(e: dict[str, Any]) -> str:
    return plabel(e["latest"]["period"], e.get("period_type"))


def _level(e: dict[str, Any]) -> str:
    return format_claim_id(e["id"], e["latest"]["period"], "level", e.get("dims") or {})


def _change(e: dict[str, Any]) -> str:
    return format_claim_id(e["id"], e["latest"]["period"], "change", e.get("dims") or {})


def _pct(v: float, dec: int = 1) -> str:
    return num(v, dec, "%")


def _pp(v: float, dec: int = 1) -> str:
    return num(abs(v), dec, "pp")


def _moved(c: float, up: str = "rose", down: str = "fell", dec: int = 1) -> str | None:
    """The verb for a change, or None when it rounds to nothing at the precision written."""
    if round(c, dec) == 0:
        return None
    return up if c > 0 else down


class _Title:
    def __init__(self):
        self.parts: list[str] = []
        self.claims: list[str] = []

    def add(self, text: str, *claims: str) -> "_Title":
        self.parts.append(text)
        self.claims.extend(claims)
        return self

    def done(self) -> Any:
        text = "".join(self.parts).strip()
        return block(text[0].upper() + text[1:], self.claims) if text else None


def _rate_move(t: _Title, e: dict[str, Any], what: str, after: str = "", *, unit: str = "%", dec: int = 1) -> None:
    """'<what> was 14.5%<after>, 1.6 pp higher than a month earlier': the level bound as a level, the
    move bound as a change, and the comparison named from the metric's own comparison basis."""
    c = _c(e)
    value = num(_v(e), dec, unit) if unit != "pp" else f"{num(_v(e), dec)} pp"
    t.add(f"{what} was {value}{after}", _level(e))
    if c is not None and _moved(c, dec=dec):
        t.add(f", {_pp(c, dec)} {'higher' if c > 0 else 'lower'} than {COMPARED.get(e.get('compare'), 'the prior period')}", _change(e))


def _m04(fp):
    g, n, o = _e(fp, "ssc.hl.gdp.growth"), _e(fp, "ssc.hl.gdp_nonoil.growth"), _e(fp, "ssc.hl.gdp_oil.growth")
    if not (g and n):
        return None
    t = _Title().add(f"Real GDP growth was {_pct(_v(g))} in {_per(g)} (YTD y/y): non-oil {_pct(_v(n))}", _level(g), _level(n))
    if o:
        t.add(f", oil-gas {_pct(_v(o))}", _level(o))
    return t.done()


def _m05(fp):
    cpi, real = _e(fp, "ssc.cpi.all.yoy"), _e(fp, "ssc.real_wage_growth_est")
    if not cpi:
        return None
    t = _Title()
    _rate_move(t, cpi, "Inflation", f" y/y in {_per(cpi)}")
    if real:
        t.add(f"; real wages grew an estimated {_pct(_v(real))} ({_per(real)})", _level(real))
    return t.done()


def _m06(fp):
    rows = [r for r in ((fp.get("slides") or {}).get("M06") or {}).get("rows") or [] if r.get("latest") and r["latest"].get("value") is not None]
    if len(rows) < 2:
        return None
    rows = sorted(rows, key=lambda r: -r["latest"]["value"])
    top, bottom = rows[0], rows[-1]
    t = _Title().add(f"{top['row_label']} led real growth at {_pct(top['latest']['value'])}; {bottom['row_label'][0].lower() + bottom['row_label'][1:]} "
                     f"was weakest at {_pct(bottom['latest']['value'])} ({_per(top)})", _level(top), _level(bottom))
    return t.done()


def _m07(fp):
    ex, rs = _e(fp, "ssc.hl.exports.level"), _e(fp, "cba.reserves.official_usd")
    if not (ex and rs):
        return None
    t = _Title()
    c = _c(ex)
    if c is not None and _moved(c, dec=0):
        t.add(f"Exports were {money(_v(ex), 'USD mln')} in {_per(ex)}, {'up' if c > 0 else 'down'} {money(abs(c), 'USD mln')} y/y", _level(ex), _change(ex))
    else:
        t.add(f"Exports were {money(_v(ex), 'USD mln')} in {_per(ex)}", _level(ex))
    t.add(f"; CBA official reserves stood at {money(_v(rs), 'USD mln')} at {_per(rs)}", _level(rs))
    return t.done()


def _m08(fp):
    y, hh = _e(fp, "cba.loans.total_ci.yoy"), _e(fp, "cba.loans.sector.households.contrib")
    if not y:
        return None
    t = _Title()
    _rate_move(t, y, "Loan growth", f" y/y at {_per(y)}")
    if hh:
        t.add(f"; households contributed {num(_v(hh), 1)} pp", _level(hh))
    return t.done()


def _m23(fp):
    r, c2g = _e(fp, "cba.loans.total_ci.real.yoy"), _e(fp, "cba.credit_to_gdp")
    if not r:
        return None
    t = _Title().add(f"Real loan growth was {_pct(_v(r))} y/y at {_per(r)}", _level(r))
    c = _c(r)
    if c is not None and _moved(c):
        t.add(f", {'up' if c > 0 else 'down'} {_pp(c)} on a year earlier", _change(r))
    if c2g:
        t.add(f"; credit-to-GDP reached {_pct(_v(c2g))} in {_per(c2g)}", _level(c2g))
    return t.done()


def _m24(fp):
    f, s = _e(fp, "cba.new_loans.total.3m.yoy"), _e(fp, "cba.loans.total_ci.yoy")
    if not f:
        return None
    side = "above" if _v(f) >= 0 else "below"
    t = _Title().add(f"New loans in the three months to {_per(f)} were {_pct(abs(_v(f)))} {side} the same months a year earlier", _level(f))
    if s:
        t.add(f", against loan-stock growth of {_pct(_v(s))}", _level(s))
    return t.done()


def _m10(fp):
    n, r = _e(fp, "cba.bank.npl.total"), _e(fp, "cba.bank.npl.ratio")
    if not (n and r):
        return None
    c = _c(n)
    t = _Title()
    if c is not None and _moved(c, dec=0):
        t.add(f"NPLs {'rose' if c > 0 else 'fell'} {money(abs(c), 'AZN mln')} in a year to {money(_v(n), 'AZN mln')}", _change(n), _level(n))
    else:
        t.add(f"NPLs were {money(_v(n), 'AZN mln')}", _level(n))
    t.add(f"; the NPL ratio was {_pct(_v(r))} at {_per(r)}", _level(r))
    return t.done()


def _m25(fp):
    cov, al = _e(fp, "cba.bank.allowance_to_npl"), _e(fp, "cba.bank.allowance_to_gross_loans")
    if not cov:
        return None
    t = _Title().add(f"Loan-loss allowances equal {_pct(_v(cov), 0)} of NPLs at {_per(cov)}", _level(cov))
    c = _c(cov)
    if c is not None and _moved(c):
        t.add(f", {'up' if c > 0 else 'down'} {_pp(c)} on a year earlier", _change(cov))
    if al:
        t.add(f"; the allowance is {_pct(_v(al))} of gross loans", _level(al))
    return t.done()


def _m11(fp):
    y, hh = _e(fp, "cba.deposits.total.yoy"), _e(fp, "cba.deposits.hh.total.yoy")
    if not y:
        return None
    t = _Title()
    _rate_move(t, y, "Deposit growth", f" y/y at {_per(y)}")
    if hh:
        t.add(f"; household deposits grew {_pct(_v(hh))}", _level(hh))
    return t.done()


def _m26(fp):
    hh, fin, nfc = _e(fp, "cba.deposits.hh.contrib"), _e(fp, "cba.deposits.fin.contrib"), _e(fp, "cba.deposits.nfc.contrib")
    if not (hh and nfc):
        return None
    t = _Title().add(f"Of total deposit growth at {_per(hh)}, households contributed {num(_v(hh), 1)} pp", _level(hh))
    if fin:
        t.add(f", financial corporations {num(_v(fin), 1)} pp", _level(fin))
    t.add(f" and non-financial corporations {num(_v(nfc), 1)} pp", _level(nfc))
    return t.done()


def _m12(fp):
    d, l = _e(fp, "cba.deposits.fx_share"), _e(fp, "cba.loans.fx_share")
    if not d:
        return None
    t = _Title()
    _rate_move(t, d, "The FX share of deposits", f" at {_per(d)}")
    if l:
        t.add(f"; the FX share of loans was {_pct(_v(l))}", _level(l))
    return t.done()


def _m27(fp):
    l, d = _e(fp, "cba.loans.fx_share"), _e(fp, "cba.deposits.fx_share")
    if not (l and d):
        return None
    t = _Title()
    _rate_move(t, l, "The FX share of loans", f" at {_per(l)}")
    t.add("; ")
    _rate_move(t, d, "the FX share of deposits")
    return t.done()


def _m13(fp):
    sp = _e(fp, 'cba.rates.new.spread|{"currency": "AZN"}')
    dep, loan = _e(fp, 'cba.rates.new.deposit|{"currency": "AZN"}'), _e(fp, 'cba.rates.new.loan|{"currency": "AZN"}')
    if not sp:
        return None
    t = _Title()
    c = _c(sp)
    verb = _moved(c, "widened", "narrowed") if c is not None else None
    if verb:
        t.add(f"The AZN pricing spread {verb} {_pp(c)} in a year to {num(_v(sp), 1)} pp", _change(sp), _level(sp))
    else:
        t.add(f"The AZN pricing spread was {num(_v(sp), 1)} pp", _level(sp))
    if dep and loan:
        t.add(f": new term deposits {_pct(_v(dep))}, new loans {_pct(_v(loan))} ({_per(sp)})", _level(dep), _level(loan))
    return t.done()


def _m28(fp):
    r, dep, m2 = _e(fp, "cba.policy.rate.month_end"), _e(fp, 'cba.rates.new.deposit|{"currency": "AZN"}'), _e(fp, "cba.money.m2.yoy")
    if not (r and dep):
        return None
    t = _Title().add(f"The refinancing rate was {num(_v(r), 2, '%')} at {_per(r)}", _level(r))
    c = _c(r)
    if c is not None and round(c * 100) != 0:
        t.add(f", {'up' if c > 0 else 'down'} {abs(c) * 100:.0f} bp in a year", _change(r))
    cd = _c(dep)
    verb = _moved(cd) if cd is not None else None
    if verb:
        t.add(f"; the new AZN deposit rate {verb} {_pp(cd)} to {_pct(_v(dep))}", _change(dep), _level(dep))
    else:
        t.add(f"; the new AZN deposit rate was {_pct(_v(dep))}", _level(dep))
    if m2:
        t.add(f" and M2 grew {_pct(_v(m2))} y/y", _level(m2))
    return t.done()


def _m14(fp):
    gap, ldr = _e(fp, "cba.loans_minus_deposits.yoy_growth_gap"), _e(fp, "cba.ldr")
    if not gap:
        return None
    t = _Title()
    g = _v(gap)
    t.add(f"{'Loans' if g >= 0 else 'Deposits'} grew {num(abs(g), 1)} pp faster than {'deposits' if g >= 0 else 'loans'} at {_per(gap)}", _level(gap))
    if ldr:
        t.add("; ")
        _rate_move(t, ldr, "the loan-to-deposit ratio")
    return t.done()


def _m15(fp):
    p, y, cti = _e(fp, "cba.bank.pnl.net_profit"), _e(fp, "cba.bank.pnl.net_profit.yoy"), _e(fp, "cba.bank.pnl.cost_to_income")
    if not p:
        return None
    t = _Title().add(f"Net profit was {money(_v(p), 'AZN mln')} in {_per(p)}", _level(p))
    if y:
        t.add(f", {'up' if _v(y) >= 0 else 'down'} {_pct(abs(_v(y)))} y/y", _level(y))
    if cti:
        t.add("; ")
        _rate_move(t, cti, "cost-to-income")
    return t.done()


def _m29(fp):
    nii = _e(fp, "cba.bank.pnl.net_interest_income.yoy_change")
    prov, tax = _e(fp, "cba.bank.pnl.provisions.yoy_change"), _e(fp, "cba.bank.pnl.tax.yoy_change")
    if not nii:
        return None
    t = _Title().add(f"Net interest income was {money(abs(_v(nii)), 'AZN mln')} {'higher' if _v(nii) >= 0 else 'lower'} than a year earlier "
                     f"({_per(nii)})", _level(nii))
    if prov:
        t.add(f"; provision charges {money(abs(_v(prov)), 'AZN mln')} {'higher' if _v(prov) >= 0 else 'lower'}", _level(prov))
    if tax:
        t.add(f" and profit tax {money(abs(_v(tax)), 'AZN mln')} {'higher' if _v(tax) >= 0 else 'lower'}", _level(tax))
    return t.done()


def _m16(fp):
    eq, liq = _e(fp, "cba.bank.equity_to_assets"), _e(fp, "cba.bank.liquid_assets_ratio")
    if not (eq and liq):
        return None
    t = _Title()
    _rate_move(t, eq, "Capital/assets", f" at {_per(eq)}")
    t.add("; ")
    _rate_move(t, liq, "liquid assets/assets")
    return t.done()


def _m19(fp):
    """'The CBA kept the refinancing rate at 6.50% on 23 Sep 2026 and cut the corridor floor 50 bp to 5.00%'."""
    rate, floor, ceiling = _e(fp, "cba.policy.rate"), _e(fp, "cba.policy.corridor_floor"), _e(fp, "cba.policy.corridor_ceiling")
    if not rate or rate.get("compare") != "previous_observation":
        return None
    d = rate["latest"]["period"]
    when = f"{int(d[8:10])} {FULL_MONTHS[int(d[5:7]) - 1]} {d[:4]}"      # the day is read as a date only before a full month name
    c = _c(rate)
    t = _Title()
    if c is None or round(c * 100) == 0:
        t.add(f"The CBA kept the refinancing rate at {num(_v(rate), 2, '%')} on {when}", _level(rate))
    else:
        t.add(f"The CBA {'raised' if c > 0 else 'cut'} the refinancing rate {abs(c) * 100:.0f} bp to {num(_v(rate), 2, '%')} on {when}",
              _change(rate), _level(rate))
    moved = []
    for e, label in ((floor, "floor"), (ceiling, "ceiling")):
        if e and e["latest"]["period"] == d and _c(e) is not None and round(_c(e) * 100) != 0:
            moved.append((e, label))
    for i, (e, label) in enumerate(moved):
        t.add(f"{' and' if i == 0 else ', and'} {'raised' if _c(e) > 0 else 'cut'} the corridor {label} {abs(_c(e)) * 100:.0f} bp "
              f"to {num(_v(e), 2, '%')}", _change(e), _level(e))
    return t.done()


TITLES: dict[str, Callable[[dict[str, Any]], Any]] = {
    "M19": _m19,
    "M04": _m04, "M05": _m05, "M06": _m06, "M07": _m07, "M08": _m08, "M23": _m23, "M24": _m24, "M10": _m10, "M25": _m25,
    "M11": _m11, "M26": _m26, "M12": _m12, "M27": _m27, "M13": _m13, "M28": _m28, "M14": _m14, "M15": _m15, "M29": _m29,
    "M16": _m16,
}


def finding_titles(fp: dict[str, Any]) -> dict[str, Any]:
    """{slide id: title block} for every slide whose inputs are in the fact pack."""
    out: dict[str, Any] = {}
    for sid, make in TITLES.items():
        try:
            title = make(fp)
        except (KeyError, TypeError, ValueError, IndexError):
            title = None
        if title:
            out[sid] = title
    return out
