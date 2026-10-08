"""The one-page infographic: every figure comes from the fact pack, every figure keeps its own
period, a policy decision whose figures are not yet confirmed is never given the previous
decision's figures, and the page fits on one A4 sheet."""
import html as htmllib
import math
import re
import subprocess
from datetime import date

import pytest

from azmonitor.render import onepager as op

MONTH_ENDS = [date(2024 + (m // 12), m % 12 + 1, 1) for m in range(8, 32)]      # Sep 2024 .. Aug 2026


def _month_end(d: date) -> str:
    nxt = date(d.year + (d.month // 12), d.month % 12 + 1, 1)
    return date.fromordinal(nxt.toordinal() - 1).isoformat()


PERIODS = [_month_end(d) for d in MONTH_ENDS]


def _points(last: float, wobble: float) -> list[list]:
    return [[p, round(last + wobble * math.sin(i / 3.0) - wobble * math.sin(23 / 3.0), 6)] for i, p in enumerate(PERIODS)]


def _metric(mid, latest, prior, change, *, period="2026-08-31", prior_period="2026-07-31", period_type="monthly",
            compare="lag1", label=None, latest_label=None, prior_label=None):
    month = lambda iso: f"{op.MONTHS[int(iso[5:7]) - 1]} {iso[:4]}"
    return {"id": mid, "label": label or mid, "period_type": period_type, "compare": compare, "available": True,
            "latest": {"period": period, "value": latest, "period_label": latest_label or month(period)},
            "prior": {"period": prior_period, "value": prior, "period_label": prior_label or month(prior_period)},
            "change": change}


def pack(*, decision=None, cpi_period="2026-08-31", loans_period="2026-08-31") -> dict:
    lag12 = dict(prior_period="2025-08-31", compare="lag12", period_type="month_end_stock",
                 latest_label="end-Aug 2026", prior_label="end-Aug 2025")
    metrics = {
        "scorecard.non_oil_growth": _metric("ssc.hl.gdp_nonoil.growth", 2.1, 2.2, -0.1, period_type="ytd_growth_yoy",
                                            compare="prior_edition"),
        "scorecard.gdp_growth": _metric("ssc.hl.gdp.growth", 1.2, 1.4, -0.2, period_type="ytd_growth_yoy",
                                        compare="prior_edition"),
        "scorecard.cpi_yoy": _metric("ssc.cpi.all.yoy", 5.7, 5.8, -0.1, period=cpi_period,
                                     prior_period=PERIODS[PERIODS.index(cpi_period) - 1]),
        "scorecard.loans_yoy": _metric("cba.loans.total_ci.yoy", 14.507, 12.877, 1.63, period=loans_period,
                                       prior_period=PERIODS[PERIODS.index(loans_period) - 1]),
        "scorecard.deposits_yoy": _metric("cba.deposits.total.yoy", 11.13, 6.52, 4.61, period=loans_period,
                                          prior_period=PERIODS[PERIODS.index(loans_period) - 1]),
        "scorecard.deposit_fx_share": _metric("cba.deposits.fx_share", 35.54, 38.742, -3.202, **lag12),
        "scorecard.npl_ratio": _metric("cba.bank.npl.ratio", 2.88, 2.75, 0.13, **lag12),
        "scorecard.liquid_assets": _metric("cba.bank.liquid_assets_ratio", 29.34, 33.43, -4.09, **lag12),
        "scorecard.net_profit": _metric("cba.bank.pnl.net_profit", 949.68, 774.30, 175.38, period_type="ytd_flow",
                                        prior_period="2025-08-31", compare="lag12"),
    }
    slides = {
        "M04": {"chart_ytd_growth": [{"id": "ssc.hl.gdp_nonoil.growth", "dims": {}, "points": _points(2.1, 0.4)}]},
        "M05": {"chart_cpi": [{"id": "ssc.cpi.all.yoy", "dims": {}, "points": _points(5.7, 1.1)},
                              {"id": "ssc.cpi.food.yoy", "dims": {}, "points": _points(7.2, 1.0)},
                              {"id": "ssc.cpi.nonfood.yoy", "dims": {}, "points": _points(3.7, 0.8)},
                              {"id": "ssc.cpi.services.yoy", "dims": {}, "points": _points(5.0, 0.6)}]},
        "M08": {"chart_yoy": [{"id": "cba.loans.total_ci.yoy", "dims": {}, "points": _points(14.507, 5.0)}]},
        "M10": {"chart_ratio": [{"id": "cba.bank.npl.ratio", "dims": {}, "points": _points(2.88, 0.2)}]},
        "M11": {"chart_yoy": [{"id": "cba.deposits.total.yoy", "dims": {}, "points": _points(11.13, 6.0)},
                              {"id": "cba.deposits.hh.total.yoy", "dims": {}, "points": _points(17.8, 3.0)},
                              {"id": "cba.deposits.nfc.total.yoy", "dims": {}, "points": _points(2.87, 4.0)}]},
        "M12": {"chart_fx": [{"id": "cba.deposits.fx_share", "dims": {}, "points": _points(35.54, 2.0)}]},
    }
    path = [{"date": "2024-07-31", "value": 7.25}, {"date": "2025-01-22", "value": 7.25},
            {"date": "2025-07-23", "value": 7.0}, {"date": "2025-10-29", "value": 6.75},
            {"date": "2026-02-04", "value": 6.5}, {"date": "2026-07-31", "value": 6.5}]
    return {
        "as_of": "2026-10-09",
        "edition": {"banking_period": loans_period, "macro_period": "2026-08-31", "cpi_period": cpi_period},
        "metrics": metrics, "slides": slides,
        "publications": {"policy": {"decision": decision if decision is not None else {
            "announcement_date": "2026-07-31", "policy_rate": 6.5, "corridor_floor": 5.5, "corridor_ceiling": 7.5,
            "rate_change_bp": 0.0, "action": "hold"}, "rate_path": path,
            "next_decision": {"date": "2026-10-29", "status": "announced"}}},
        "flags": [{"id": "loans_yoy_jump", "metric": "cba.loans.total_ci.yoy", "window": 12, "current": 14.507,
                   "comparison": 10.182, "triggered": True},
                  {"id": "overdue_ratio_rise", "metric": "cba.loans.overdue_ratio", "window": 6, "current": 2.069,
                   "comparison": 1.756, "triggered": True},
                  {"id": "quiet", "metric": "cba.bank.npl.ratio", "window": 12, "current": 2.88, "comparison": 2.75,
                   "triggered": False}],
    }


NARRATIVE = {"mode": "facts_only", "findings": [
    {"rank": 1, "metric_refs": ["cba.bank.pnl.net_profit"], "statement": {"text": "Bank net profit ..."}},
    {"rank": 2, "metric_refs": ["cba.bank.liquid_assets_ratio"], "statement": {"text": "Liquid assets ..."}}]}

PENDING = {"announcement_date": "2026-09-23", "policy_rate": None, "corridor_floor": None, "corridor_ceiling": None,
           "rate_change_bp": None, "action": "cut"}


def _visible_text(page: str) -> str:
    body = page[page.index("<body>"):]
    body = re.sub(r'<text[^>]*class="tick"[^>]*>[^<]*</text>', " ", body)       # axis ticks
    body = re.sub(r'<div class="n"[^>]*>\d</div>', " ", body)                   # list numbers
    return re.sub(r"\s+", " ", htmllib.unescape(re.sub(r"<[^>]+>", " ", body)))


def _pack_values(fp: dict) -> set[float]:
    out: set[float] = set()

    def walk(o):
        if isinstance(o, bool):
            return
        if isinstance(o, (int, float)):
            out.add(float(o))
        elif isinstance(o, dict):
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)
    walk(fp)
    return out


def test_every_figure_on_the_page_is_a_value_in_the_fact_pack():
    fp = pack()
    text = _visible_text(op.build_html(fp, NARRATIVE))
    text = re.sub(rf"\b(?:\d{{1,2}} )?(?:{op._MON}|January|February|March|April|June|July|August|September|October|"
                  rf"November|December)(?:–(?:{op._MON}))? \d{{4}}\b", " ", text)      # dates and periods
    text = re.sub(r"\bin \d+ months\b", " ", text)                                      # a flag's window
    values = _pack_values(fp)
    figures = re.findall(r"[−+]?\d[\d,]*(?:\.\d+)?", text)
    assert len(figures) > 30
    for fig in figures:
        x = float(fig.replace("−", "-").replace("+", "").replace(",", ""))
        decimals = len(fig.split(".")[1]) if "." in fig else 0
        assert any(round(abs(v), decimals) == abs(x) for v in values), f"{fig} is not a value in the fact pack"


def test_a_decision_without_confirmed_figures_is_never_given_the_previous_decisions_figures():
    fp = pack(decision=PENDING)
    page = op.build_html(fp, NARRATIVE)
    assert "decided 31 Jul 2026" in page
    assert "Figures from the 23&nbsp;Sep decision are pending" in page or "Figures from the 23 Sep decision are pending" in page
    assert "Unchanged" not in page and "Cut " not in page                 # the keyword reading "cut" is not reported
    assert "The CBA" not in op._headline(fp)
    assert "23 Sep 2026" in page                                     # the decision itself is acknowledged


def test_the_rate_line_stops_where_a_newer_decision_has_no_figures_yet():
    window = [(p, 5.0) for p in PERIODS]
    confirmed = op.policy_view(pack())
    pts = op._rate_points(confirmed, window)
    assert pts[0] == (PERIODS[0], 7.25) and pts[-1] == ("2026-08-31", 6.5)
    inside = op.policy_view(pack(decision=dict(PENDING, announcement_date="2026-08-20")))
    assert op._rate_points(inside, window)[-1] == ("2026-07-31", 6.5)  # not carried past an unconfirmed decision
    after = op.policy_view(pack(decision=PENDING))                       # 23 Sep is after the chart's last month
    assert op._rate_points(after, window)[-1] == ("2026-08-31", 6.5)


def test_a_confirmed_hold_and_a_cut_read_as_such():
    page = op.build_html(pack(), NARRATIVE)
    assert "Unchanged" in page and "Corridor 5.50–7.50%" in page
    assert "The CBA kept its policy rate at <b>6.50%</b> on 31 Jul 2026." in op._headline(pack())
    cut = pack(decision={"announcement_date": "2026-07-31", "policy_rate": 6.5, "corridor_floor": 5.5,
                         "corridor_ceiling": 7.5, "rate_change_bp": -25.0, "action": "cut"})
    assert "Cut 25&nbsp;bp" in op.build_html(cut, NARRATIVE) or "Cut 25 bp" in op.build_html(cut, NARRATIVE)
    assert "cut its policy rate to <b>6.50%</b>" in op._headline(cut)


def test_each_figure_in_the_headline_keeps_its_own_month():
    same = op._headline(pack())
    assert same.startswith("In August, inflation eased to <b>5.7%</b>, loan growth picked up to <b>14.5%</b> "
                           "and deposit growth to <b>11.1%</b>.")
    mixed = op._headline(pack(loans_period="2026-07-31"))
    assert "In August, inflation eased to <b>5.7%</b>." in mixed
    assert "In July, loan growth picked up to <b>14.5%</b> and deposit growth to <b>11.1%</b>." in mixed


def test_a_change_is_coloured_only_where_the_scorecard_says_which_way_is_good():
    tiles = {t["title"]: t for t in op._tiles(pack())}
    assert tiles["Inflation"]["tone"] == "good"                  # down is good
    assert tiles["Non-performing loans"]["tone"] == "bad"         # up is bad
    assert tiles["Loans to the economy"]["tone"] == "neutral"     # no preferred direction
    assert tiles["Non-oil GDP growth"]["tone"] == "bad"
    assert tiles["Non-oil GDP growth"]["period"] == "Jan–Aug 2026"
    assert tiles["Non-oil GDP growth"]["chip"] == "−0.1 pp vs Jan–Jul"
    assert tiles["Non-performing loans"]["chip"] == "+0.1 pp vs Aug 2025"


def test_findings_already_on_a_tile_are_not_repeated_and_untriggered_flags_are_not_shown():
    page = op.build_html(pack(), NARRATIVE)
    things = re.findall(r'<div class="t">([^<]*)</div>', page)
    assert things[0] == "Liquid assets lower than a year earlier"       # profit is a tile, so skipped
    assert len(things) == 3
    assert "Loan growth" in page and "Overdue loans" in page
    assert op._signals(pack())[1] == {"name": "Overdue loans", "theme": "lending", "from": "1.8%", "to": "2.1%",
                                      "months": 6}
    assert len(op._signals(pack())) == 2


def test_month_ticks_end_on_the_latest_month():
    assert op._month_ticks(PERIODS) == ["2025-02-28", "2025-08-31", "2026-02-28", "2026-08-31"]


def test_figures_stay_with_their_units_and_periods_with_their_years():
    out = op._text("4.1 pp lower at end-Aug 2026; +175.4 mln AZN in Jan–Aug 2026")
    assert "4.1&nbsp;pp" in out and "mln&nbsp;AZN" in out
    assert '<span class="nw">end-Aug&nbsp;2026</span>' in out and '<span class="nw">Jan–Aug&nbsp;2026</span>' in out


def test_a_long_analyst_finding_is_shortened_to_its_first_sentence_never_cut_mid_sentence():
    first = "Liquid assets were 29.3% of bank assets at end-Aug 2026, 4.1 pp lower than a year earlier."
    long_text = first + " Banks moved more of their balance sheets into loans while deposits grew more slowly." * 3
    analyst = {"mode": "analyst_file", "findings": [
        {"rank": 1, "metric_refs": ["cba.bank.liquid_assets_ratio"], "statement": {"text": long_text}}]}
    assert op._takeaways(pack(), analyst, set())[0]["text"] == first
    one_sentence = "Liquid assets were 29.3% of bank assets " + "and more words " * 20 + "at end-Aug 2026."
    analyst["findings"][0]["statement"]["text"] = one_sentence
    assert op._takeaways(pack(), analyst, set())[0]["text"] == one_sentence


def _open_sans() -> bool:
    """The layout is measured with the font the report runners install; with another font the
    measurement would be of a different page."""
    try:
        out = subprocess.run(["fc-list", ":family=Open Sans"], capture_output=True, text=True, timeout=30).stdout
    except (OSError, subprocess.SubprocessError):
        return False
    return "Open Sans" in out


@pytest.mark.skipif(op._chromium() is None or not _open_sans(), reason="needs Chromium and the Open Sans font")
def test_the_page_fits_one_a4_sheet_and_long_text_shortens_the_charts_instead(tmp_path):
    pdfium = pytest.importorskip("pypdfium2")
    result = op.render_onepager(pack(), NARRATIVE, tmp_path)
    assert result["layout"]["fits"], result["layout"]
    assert result["layout"]["signals_shown"]
    base_height = result["layout"]["chart_height"]
    doc = pdfium.PdfDocument(result["pdf"])
    assert len(doc) == 1 and [round(x) for x in doc[0].get_size()] == [595, 842]
    doc.close()
    long_sentence = ("Liquid assets were 29.3% of bank assets at end-Aug 2026, 4.1 pp lower than a year earlier, "
                     "as banks moved more of their balance sheets into loans to households and companies while "
                     "deposit growth picked up only in the last two months of the period.")
    analyst = {"mode": "analyst_file", "findings": [
        {"rank": 1, "metric_refs": ["cba.bank.liquid_assets_ratio"], "statement": {"text": long_sentence}}]}
    longer = op.render_onepager(pack(), analyst, tmp_path / "long")
    assert longer["layout"]["fits"], longer["layout"]
    assert longer["layout"]["chart_height"] < base_height        # the charts gave way, not the text
    shown = re.sub(r"\s+", "", _visible_text(open(longer["html"], encoding="utf-8").read()))
    assert re.sub(r"\s+", "", long_sentence) in shown              # one long sentence is kept whole
