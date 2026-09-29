/**
 * Tests for the formatting that decides how a figure is labelled.
 *
 * This file exists because of one bug: `period()` used to test whether the period type *contained*
 * "year", so `monthly_index_vs_same_month_prev_year` — the type CPI inflation carries — rendered a
 * monthly reading as a calendar year. The number on screen was right and its label was wrong, which
 * is the worst combination available and the hardest to notice. Every period type in the dataset is
 * pinned here so that cannot come back.
 */
import test from "node:test";
import assert from "node:assert/strict";
import { period, provenance, signed, withUnit, sourceName, reportTitle } from "../lib/format.ts";

// Every distinct period_type in the published read model, with what a reader should see.
const PERIOD_CASES: [string, string, string][] = [
  ["monthly_index_vs_same_month_prev_year", "2026-08-31", "August 2026"],
  ["month_end_stock_growth_yoy", "2026-07-31", "July 2026"],
  ["month_end_stock", "2026-07-31", "July 2026"],
  ["month_end_average_rate", "2026-07-31", "July 2026"],
  ["monthly_average_rate", "2026-07-31", "July 2026"],
  ["monthly_flow", "2026-07-31", "July 2026"],
  ["monthly_flow_growth_yoy", "2026-07-31", "July 2026"],
  ["monthly_growth_yoy", "2026-07-31", "July 2026"],
  ["monthly_index", "2026-07-31", "July 2026"],
  ["monthly_index_growth_yoy", "2026-07-31", "July 2026"],
  ["monthly_index_vs_prev_month", "2026-07-31", "July 2026"],
  ["ytd_flow", "2026-07-31", "Jan–July 2026 (cumulative)"],
  ["ytd_average", "2026-07-31", "Jan–July 2026 (cumulative)"],
  ["ytd_growth_yoy", "2026-08-31", "Jan–August 2026 (cumulative)"],
  ["ytd_index_vs_prev_year", "2026-08-31", "Jan–August 2026 (cumulative)"],
  ["ytd_average_index_vs_prev_year", "2026-08-31", "Jan–August 2026 (cumulative)"],
  ["ytd_flow_growth_yoy", "2026-08-31", "Jan–August 2026 (cumulative)"],
  ["ytd_flow_prev_year_comparable", "2026-08-31", "Jan–August 2026 (cumulative)"],
  ["annual_average", "2025-12-31", "2025"],
  ["annual_flow", "2025-12-31", "2025"],
  ["annual_index_vs_prev_year", "2025-12-31", "2025"],
  // An annualised *rate* is observed at a month end. It is not a year.
  ["annualised_ratio", "2026-07-31", "July 2026"],
  ["quarterly_flow", "2026-06-30", "Q2 2026"],
  ["quarterly_flow_growth_yoy", "2026-06-30", "Q2 2026"],
  ["quarter_end_ratio", "2026-03-31", "Q1 2026"],
  ["rolling3_monthly_flow", "2026-07-31", "3 months to Jul 2026"],
  ["rolling4_quarterly_flow", "2026-06-30", "4 quarters to Q2 2026"],
  ["period_end_ratio", "2026-07-31", "July 2026"],
  ["stock_growth_yoy", "2026-07-31", "July 2026"],
  ["stock_index_vs_prev_year", "2026-07-31", "July 2026"],
  ["policy_rate_effective", "2026-07-31", "effective 31 Jul 2026"],
  ["forecast", "2027-12-31", "projected for December 2027"],
  ["stress_test_projection", "2027-12-31", "stress horizon December 2027"],
];

test("every period type in the dataset is labelled for the period it covers", () => {
  for (const [type, end, expected] of PERIOD_CASES) {
    assert.equal(period(end, type), expected, `${type} @ ${end}`);
  }
});

test("a year-on-year monthly reading is never labelled as a year", () => {
  // The original bug, stated as its own case.
  assert.equal(period("2026-08-31", "monthly_index_vs_same_month_prev_year"), "August 2026");
  assert.notEqual(period("2026-08-31", "monthly_index_vs_same_month_prev_year"), "2026");
});

test("a missing period says so rather than guessing", () => {
  assert.equal(period(null), "period not recorded");
  assert.equal(period(undefined), "period not recorded");
  assert.equal(period("not-a-date"), "not-a-date");
});

test("an unknown period type falls back to the month, not to silence", () => {
  assert.equal(period("2026-07-31", "something_new"), "July 2026");
  assert.equal(period("2026-07-31", null), "July 2026");
});

test("units are rendered at the precision they deserve", () => {
  assert.equal(withUnit(5.7, "%"), "5.7%");
  assert.equal(withUnit(-0.14, "pp"), "−0.1 pp");
  assert.equal(withUnit(34155.081826338, "AZN mln"), "34,155 AZN mln");
  assert.equal(withUnit(null, "%"), "—");
  assert.equal(withUnit(undefined, "%"), "—");
});

test("a signed change carries a true minus sign, not a hyphen", () => {
  assert.equal(signed(3.7), "+3.7");
  assert.equal(signed(-0.1), "−0.1");
  assert.equal(signed(0), "0.0");
});

test("the publishing body is named, not abbreviated", () => {
  assert.equal(sourceName("cba"), "Central Bank of Azerbaijan");
  assert.equal(sourceName(null, "ssc.cpi.all.yoy"), "State Statistical Committee");
  assert.equal(sourceName(null, undefined), "unattributed source");
});

test("provenance always states the period, the origin and the source", () => {
  const lines = provenance({
    series_id: "cba.loans.total_ci.yoy",
    period_end: "2026-07-31",
    period_type: "month_end_stock_growth_yoy",
    kind: "observation",
    origin: "computed",
    formula: "yoy_growth",
    source_id: "cba",
    published_at: "2026-09-09",
  });
  assert.equal(lines[0], "Observed · July 2026");
  // A derived figure must never read as something the Central Bank published.
  assert.equal(lines[1], "Computed here (yoy_growth) from Central Bank of Azerbaijan");
  assert.equal(lines[2], "Published 9 Sep 2026");
});

test("a published figure is not labelled as computed", () => {
  const lines = provenance({
    series_id: "cba.loans.total_ci",
    period_end: "2026-07-31",
    period_type: "month_end_stock",
    kind: "observation",
    origin: "source",
    source_id: "cba",
  });
  assert.equal(lines[1], "Source: Central Bank of Azerbaijan");
});

test("forecasts and stress results are labelled as what they are", () => {
  assert.equal(
    provenance({ kind: "forecast", period_end: "2027-12-31", period_type: "forecast" })[0],
    "Forecast · projected for December 2027",
  );
  assert.equal(
    provenance({ kind: "stress", period_end: "2027-12-31", period_type: "stress_test_projection" })[0],
    "Stress scenario · stress horizon December 2027",
  );
});

test("report titles come from the engine's own names when it has published them", () => {
  const titles = { monthly: "Macro & Banking Monitor", mpr_brief: "Monetary Policy Review brief" };
  assert.equal(reportTitle("monthly", titles), "Macro & Banking Monitor");
  assert.equal(reportTitle("mpr_brief", titles), "Monetary Policy Review brief");
  // A type the engine gained since the last publish still reads sensibly.
  assert.equal(reportTitle("new_brief", titles), "New Brief");
  assert.equal(reportTitle("monthly"), "Monthly");
});
