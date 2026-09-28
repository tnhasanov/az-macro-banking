# Data-quality exceptions and failing checks

Generated 2026-09-19 from `monitor validate`. Checks: 40, failing: 5 (critical 0, warning 5, accepted exceptions 0).

A **critical** failure touches a period this edition displays and blocks publication. A **warning** is confined to history the edition does not show: it is recorded here and in appendix A02, and no published figure depends on it. An **accepted exception** is an explicit entry in `config/quality_exceptions.yaml` naming the periods, the reason and the reviewer.

Display window for this assessment: from 2023-07-01 (36 months to the banking anchor 2026-07-31).

Correction to an earlier description of these failures: they are **not** all before the start of the collected history. The dataset starts in 2020 and one failing period, 2021-10-31, is inside it. What is true of every current failure is narrower and is what this document states: none of the failing periods falls inside the window the current edition displays or compares against.

## components_sum:cba_loans_by_institution

* Severity: **warning**
* Failing periods: 2006-01-31
* Failures / comparisons: 1 of 259
* Note: the failing periods are outside the window this edition displays; the discrepancy is recorded and reported, and no published figure in this edition depends on it
* Check definition: total `cba.loans.total_ci` = sum of `cba.loans.state_banks`, `cba.loans.private_banks`, `cba.loans.nbci` (tolerance 0.5 AZN mln)

**Evidence, 2006-01-31** — the check as configured (values as stored, unrounded):

| Role | Series | Value | Source cell |
|---|---|---:|---|
| published total | `cba.loans.total_ci` | 1,380.360 | 2.6!B32 |
| component | `cba.loans.state_banks` | 684.298 | 2.6!C32 |
| component | `cba.loans.private_banks` | 656.450 | 2.6!E32 |
| component | `cba.loans.nbci` | 48.290 | 2.6!K32 |

Sum of the configured components 1,389.038; published total 1,380.360; difference -8.678 (tolerance 0.5). The parser reads exactly the cells shown, so the difference is in the source file rather than in extraction.


Impact: the discrepancy is 8.678 AZN mln on `cba.loans.total_ci` for 2006-01-31. It affects no figure in this edition: the period is outside the displayed window and outside every comparison the deck makes.


## components_sum:cba_loans_by_maturity

* Severity: **warning**
* Failing periods: 2010-07-31
* Failures / comparisons: 1 of 518
* Note: the failing periods are outside the window this edition displays; the discrepancy is recorded and reported, and no published figure in this edition depends on it
* Check definition: total `cba.loans.total_ci_m` = sum of `cba.loans.azn`, `cba.loans.fx` (tolerance 0.5 AZN mln)
* Check definition: total `cba.loans.overdue` = sum of `cba.loans.azn_overdue`, `cba.loans.fx_overdue` (tolerance 0.5 AZN mln)

**Evidence, 2010-07-31** — the check as configured (values as stored, unrounded):

| Role | Series | Value | Source cell |
|---|---|---:|---|
| published total | `cba.loans.total_ci_m` | 8,656.216 | 2.7!B93 |
| component | `cba.loans.azn` | 5,357.580 | 2.7!D93 |
| component | `cba.loans.fx` | 3,293.636 | 2.7!J93 |

Sum of the configured components 8,651.216; published total 8,656.216; difference +5.000 (tolerance 0.5). The parser reads exactly the cells shown, so the difference is in the source file rather than in extraction.


Impact: the discrepancy is 5.000 AZN mln on `cba.loans.total_ci_m` for 2010-07-31. It affects no figure in this edition: the period is outside the displayed window and outside every comparison the deck makes.


## components_sum:cba_deposits

* Severity: **warning**
* Failing periods: 2015-12-31, 2016-01-31
* Failures / comparisons: 2 of 458
* Note: the failing periods are outside the window this edition displays; the discrepancy is recorded and reported, and no published figure in this edition depends on it
* Check definition: total `cba.deposits.total` = sum of `cba.deposits.hh.total`, `cba.deposits.fin.total`, `cba.deposits.nfc.total` (tolerance 0.5 AZN mln)
* Check definition: total `cba.deposits.hh.total` = sum of `cba.deposits.hh.azn_demand`, `cba.deposits.hh.azn_time`, `cba.deposits.hh.fx_demand`, `cba.deposits.hh.fx_time` (tolerance 0.5 AZN mln)

**Evidence, 2015-12-31** — the check as configured (values as stored, unrounded):

| Role | Series | Value | Source cell |
|---|---|---:|---|
| published total | `cba.deposits.total` | 23,431.389 | 2.11!B160 |
| component | `cba.deposits.hh.total` | 9,473.935 | 2.11!C160 |
| component | `cba.deposits.fin.total` | 6,358.776 | 2.11!H160 |
| component | `cba.deposits.nfc.total` | 7,630.359 | 2.11!M160 |

Sum of the configured components 23,463.071; published total 23,431.389; difference -31.681 (tolerance 0.5). The parser reads exactly the cells shown, so the difference is in the source file rather than in extraction.


Impact: the discrepancy is 31.681 AZN mln on `cba.deposits.total` for 2015-12-31. It affects no figure in this edition: the period is outside the displayed window and outside every comparison the deck makes.


**Evidence, 2016-01-31** — the check as configured (values as stored, unrounded):

| Role | Series | Value | Source cell |
|---|---|---:|---|
| published total | `cba.deposits.total` | 22,576.487 | 2.11!B162 |
| component | `cba.deposits.hh.total` | 8,643.339 | 2.11!C162 |
| component | `cba.deposits.fin.total` | 7,166.995 | 2.11!H162 |
| component | `cba.deposits.nfc.total` | 6,800.555 | 2.11!M162 |

Sum of the configured components 22,610.889; published total 22,576.487; difference -34.402 (tolerance 0.5). The parser reads exactly the cells shown, so the difference is in the source file rather than in extraction.


Impact: the discrepancy is 34.402 AZN mln on `cba.deposits.total` for 2016-01-31. It affects no figure in this edition: the period is outside the displayed window and outside every comparison the deck makes.


## components_sum:cba_deposits_currency

* Severity: **warning**
* Failing periods: 2021-10-31
* Failures / comparisons: 1 of 259
* Note: the failing periods are outside the window this edition displays; the discrepancy is recorded and reported, and no published figure in this edition depends on it
* Check definition: total `cba.deposits.cur.total` = sum of `cba.deposits.cur.azn_demand`, `cba.deposits.cur.azn_time`, `cba.deposits.cur.fx_demand`, `cba.deposits.cur.fx_time` (tolerance 0.5 AZN mln)

**Evidence, 2021-10-31** — the check as configured (values as stored, unrounded):

| Role | Series | Value | Source cell |
|---|---|---:|---|
| published total | `cba.deposits.cur.total` | 26,114.803 | 2.12!B235 |
| component | `cba.deposits.cur.azn_demand` | 8,932.012 | 2.12!C235 |
| component | `cba.deposits.cur.azn_time` | 3,751.775 | 2.12!D235 |
| component | `cba.deposits.cur.fx_demand` | 7,973.125 | 2.12!E235 |
| component | `cba.deposits.cur.fx_time` | 5,492.345 | 2.12!F235 |

Sum of the configured components 26,149.256; published total 26,114.803; difference -34.453 (tolerance 0.5). The parser reads exactly the cells shown, so the difference is in the source file rather than in extraction.

Diagnosis: `cba.deposits.cur.fx_time` carries exactly the 2021-09-30 value (5,492.345) in the 2021-10-31 row, while the published total moved. The component, not the total, is the inconsistent cell.


Impact: the discrepancy is 34.453 AZN mln on `cba.deposits.cur.total` for 2021-10-31. It affects no figure in this edition: the period is outside the displayed window and outside every comparison the deck makes.


## contributions_reconcile:cba.deposits

* Severity: **warning**
* Failing periods: 2015-12-31, 2016-01-31, 2016-12-31, 2017-01-31
* Failures / comparisons: 4 of 247
* Note: the failing periods are outside the window this edition displays; the discrepancy is recorded and reported, and no published figure in this edition depends on it

Examples:

```
[
 [
  "2015-12-31",
  51.83150637665032,
  51.626296479154774
 ],
 [
  "2016-01-31",
  49.39500923583058,
  49.16771955632808
 ],
 [
  "2016-12-31",
  -5.855781804031894,
  -5.72057391953008
 ],
 [
  "2017-01-31",
  2.675894189104005,
  2.8282758277409448
 ]
]
```

## How these are handled

1. Every check runs on every refresh and the result is stored in `data/state/quality_report.json`.
2. A critical failure blocks a new edition; the previous edition stays current and `data/state/monthly_status.json` records the cause.
3. A warning is published in appendix A02 with its periods, so a reader can see what did not reconcile.
4. Publishing despite a critical failure requires an entry in `config/quality_exceptions.yaml` that names the periods, the reason, the evidence, the impact, who accepted it and when it is reviewed.
