# Build the Azerbaijan Macro & Banking Monitor

You are a senior economic analyst, banking analyst and software engineer. Build a working system that collects official Azerbaijani statistics, maintains a historical dataset, calculates reliable indicators, writes evidence-based analysis and produces recurring editable PowerPoint decks with PDF and Excel outputs.

The audience is a bank CRO, Management Board and senior business leaders. The user works at Azer-Turk Bank (ATB). Initially, use public data from the Central Bank of Azerbaijan (CBA) and the State Statistical Committee of Azerbaijan (SSC). Discuss implications and questions relevant to a bank, but do not imply knowledge of ATB's internal exposures, financial results or risk limits.

Implement the system. A proposal or scaffold alone is not the requested result. Work in stages, demonstrate a complete first report, and document any genuine access or data limitations.

## 1. Objective and operating defaults

The system must answer:

- What changed in the economy and banking sector since the previous report?
- Which components account for the changes?
- Where do credit, funding and economic activity diverge?
- Which developments deserve a management discussion?
- What evidence would confirm or weaken the interpretation in the next release?

Defaults, configurable without editing application code:

- Geography: Azerbaijan.
- Timezone: Asia/Baku.
- Historical collection: January 2020 onward, where comparable official data can be recovered.
- Main chart window: latest 36 observations, with longer history available in the workbook.
- Output language: English initially. Support an Azerbaijani language configuration and a controlled terminology glossary. A second language must use the same calculations and source references.
- Primary report: monthly macroeconomic and banking monitor.
- Additional reports: weekly release digest and an optional sector review.
- Daily source check: 09:00 Baku time, as a configurable deployment default.
- Delivery: save reports and expose a configured report location. Keep messaging connectors optional and disabled until the user supplies a destination and authorizes delivery.
- Default template: restrained professional financial-reporting design. If an approved ATB template is supplied, support adapting the renderer to it. Do not invent an official ATB logo or brand specification.

Use the execution date and source metadata to determine the latest available data. Do not hardcode the examples or current period mentioned in this brief. This brief was prepared on 18 September 2026.

## 2. Source discovery and source hierarchy

Use these official entry points. They are discovery pages, not guaranteed API endpoints or permanent file URLs.

| Source ID | Entry point | Intended role |
| --- | --- | --- |
| CBA_MONETARY | https://www.cbar.az/page-42/monetary-indicators | Lending, deposits, rates, money aggregates, currency and maturity breakdowns |
| CBA_BANKING | https://www.cbar.az/page-188/credit-institutions | Published bank-sector overviews and other credit-institution statistics |
| CBA_BULLETIN | https://www.cbar.az/page-40/statistical-bulletin | Statistical bulletin, historical reference and reconciliation |
| CBA_CALENDAR | https://www.cbar.az/page-103/schedule-of-statistical-releases | Expected release timing |
| CBA_STATS_INDEX | https://www.cbar.az/ | Discover the current official links for macro statistics, external-sector statistics and interactive statistics |
| SSC_MACRO | https://www.stat.gov.az/news/macroeconomy.php?page=1 | Monthly macroeconomic overview and historical pages |
| SSC_OPEN_DATA | https://www.stat.gov.az/menu/6/opendata/ | Sector tables, prices, national accounts, labour market, regions and related releases |
| SSC_CALENDAR | https://www.stat.gov.az/menu/4/publications/ | Release calendar |
| SSC_RELEASES | https://www.stat.gov.az/ | Relevant official press releases and current publication links |

The entry-point listings were inspected when this brief was prepared. Complete download availability, file formats, historical coverage and table-level definitions still require verification during implementation. Do not interpret this source map as confirmation that every requested metric is published.

The CBA monetary-statistics page lists separate tables for loans by institution, maturity, economic activity and region, new lending, deposits, household savings and interest rates. Discover the current download targets by their descriptive titles. Retain original Azerbaijani titles alongside English labels.

Some CBA overview and bulletin links appear as asset anchors in extracted HTML. Resolve the actual public document targets using the page structure, public asset references or a browser when necessary. Do not assume an anchor is itself a downloadable file, guess undocumented endpoints, or invent a successful download.

Source preference:

1. Official structured downloads or documented public interfaces with usable definitions.
2. Official HTML tables.
3. Text-based official PDF tables and explanatory notes.
4. OCR only where the relevant material is scanned and no reliable structured equivalent exists.

Use CBA as the primary source for banking and monetary data, and SSC for macroeconomic and real-sector data. Where SSC republishes a CBA indicator, reconcile it rather than treating it as independent evidence. Preserve disagreements and explain differences in period, institutional coverage or revision vintage.

The first version must operate on these official sources. Additional sources, paid feeds and internal bank files are future extensions. Follow links to official publication hosts where necessary. Respect access restrictions and reasonable request rates.

Create a source register containing discovery URL, document URL, title, source institution, format, publication-date availability, expected frequency, coverage start, parser, status, definitions and known limitations. Create an availability matrix showing which requested slide inputs are available, partially available, unavailable or still unverified.

Distinguish a download listing that was discovered from a file successfully downloaded, a table successfully parsed, and an indicator successfully validated. Track expected publication lags by series. An older observation can be the latest official value; label its age and do not mistake it for a failed collection.

## 3. Data collection and historical storage

Build a repeatable collection process with caching, bounded retries, timeouts and logs. Save the original downloaded bytes and a content hash. Detect both new publications and replacements of previously downloaded files.

Use a lightweight persistent design for version one: Python for ingestion and calculations, SQLite for metadata and observation vintages, and Parquet or equivalent files for analytical tables. A single persistent data directory is sufficient. Keep the design portable to managed database and object storage later. Do not build a dashboard, vector database or complex service architecture for this MVP.

For each observation, retain at least:

- Stable series ID and original source label.
- Source institution and source-document version ID.
- Discovery URL and actual document URL.
- Table/sheet/page and row/column or cell reference, where applicable.
- Unit, currency, scale and measurement basis.
- Institutional population: banks, all credit institutions, BOKTs or another defined group.
- Sector, geography, counterparty, currency and maturity dimensions, where available.
- Reference-period start/end, frequency and period type.
- Period type such as month-end stock, monthly flow, year-to-date flow, quarterly flow, average rate or index.
- Publication date when explicitly available, retrieval time, and first observed time.
- Vintage, preliminary/revised status and methodological-break flag.
- Original value, normalized value, missing-value reason and extraction method.

Do not infer a publication date from the reporting month or retrieval time. Store unknown dates explicitly. Keep newly published periods and revisions to older periods distinguishable.

Normalize Azerbaijani number formats, decimal commas, spaces used as thousands separators, percentages and text markers. Treat a published zero, a blank, a dash and an unavailable observation according to the source's definitions. Never fill missing data with zero.

Maintain append-only observation vintages. A current table may revise earlier years. Preserve both the prior stored observations and the new version. Each generated report must point to an immutable snapshot and a run manifest so its numbers remain reproducible.

Distinguish two historical modes:

- Reconstructed history using the data available now.
- A historical information set using only data demonstrably published by a past cutoff.

Do not label reconstructed history as an authentic historical information set when original publication vintages are unavailable.

Persist release state and report state outside an ephemeral runner. A cloud run must explicitly restore and save the persistent dataset, publication hashes and output manifest. A fresh repository clone by itself does not provide reporting memory.

## 4. Calculation and interpretation rules

All arithmetic, ratios, chart data and numerical rankings must be produced by deterministic code. The language model receives calculated facts and source excerpts. It must not invent missing inputs or perform unverified arithmetic inside prose.

### 4.1 Comparability

Always align the institutional population, currency, unit, measurement basis, period and definitions before combining series. Bank-only deposits cannot silently serve as the denominator for lending by all credit institutions. Resident-only balances cannot silently be combined with all-customer balances.

Distinguish:

- Nominal growth from real growth.
- A percentage change from a percentage-point change.
- Monthly year-on-year growth from growth of a year-to-date cumulative total.
- Month-end stocks from monthly originations or cash flows.
- Monthly CPI inflation, year-on-year CPI inflation and average year-to-date inflation.
- Month-end reserves from cumulative exports.
- New-business interest rates from rates on outstanding balances.
- Foreign-currency balances in AZN equivalent from constant-exchange-rate measures.

Show the actual reference period of each indicator. Some SSC overviews contain different reporting cutoffs within one table. Never apply the page headline period to every row.

### 4.2 Core formulas

Use stored unrounded values for calculations. Round only for presentation.

- Growth: `(current / comparable_prior - 1) * 100`, only when the comparison is meaningful and the denominator is valid. Flag negative/zero bases where a growth rate would mislead.
- Share: `component / compatible_total * 100`.
- Percentage-point change: current percentage minus prior percentage.
- Contribution to aggregate loan or deposit growth: `change_in_component / prior_total * 100`, using mutually exclusive components that reconcile to the total.
- YTD flow growth: current cumulative amount versus the same cumulative period one year earlier.
- Monthly flow from cumulative data: current YTD amount minus preceding-month YTD amount from a consistent vintage. January uses the January YTD amount directly. Never subtract the previous December across a calendar-year reset.
- Real wage growth: `[(1 + nominal_wage_growth) / (1 + matched_CPI_growth) - 1] * 100`, with growth inputs expressed as fractions. Use an official real-wage measure when available. Align the wage and price comparison windows and label derived values as estimates.
- Simple pricing spread: comparable new-loan rate minus new-term-deposit rate, with currencies, maturities and coverage clearly stated. Call it an indicative pricing spread. It is not NIM or realized profitability.
- Loan-to-deposit ratio: compatible loan stock divided by compatible deposit stock. State gross/net and included counterparty definitions. Do not present it as a regulatory liquidity ratio.
- Credit-to-GDP ratio: use compatible annual or trailing-four-quarter nominal GDP. A credit stock divided by a YTD GDP flow is not an annual credit-to-GDP ratio. Omit it when a suitable denominator is unavailable.

Do not sum index values or subtract cumulative growth percentages to infer monthly real growth. Real GDP contributions require published contributions or a defensible compatible method. Current-price sector weights cannot automatically decompose chain-volume real GDP growth.

A change in the loan stock reflects originations, repayments, write-offs, reclassifications, valuation and other adjustments. Do not infer any one of these from the stock change alone. Use published new-lending data for originations, keeping its coverage separate where necessary.

### 4.3 Asset quality

Preserve the source's label and definition. Overdue loans, NPLs, Stage 3 exposures and default rates are separate concepts. Never relabel overdue loans as NPLs.

When a ratio falls, show the numerator balance as well as the ratio. If compatible inputs exist, decompose the ratio change into numerator and denominator effects using a documented exact sequence:

- Start with prior problem-loan balance `Q0`, current balance `Q1`, prior gross loans `L0` and current gross loans `L1`.
- Numerator effect in percentage points: `100 * (Q1 - Q0) / L0`.
- Denominator effect in percentage points: `100 * Q1 * (1/L1 - 1/L0)`.
- Sum must equal `100 * (Q1/L1 - Q0/L0)`.

This is an arithmetic decomposition with the numerator changed first. It does not establish cures, recoveries or underwriting improvement. Explain that the allocation depends on the chosen sequence.

Calculate provision coverage or cost of risk only when the appropriate allowance/expense, exposure definitions and periods exist. An allowance stock is not an impairment expense. Do not infer loan vintages, migration rates or borrower-level distress from sector aggregates.

### 4.4 Profitability and capital

Prefer published ratios with their official definitions. For derived ratios, record the full formula and annualization method. ROA/ROE require appropriate average balances. Derived NIM requires net interest income and a compatible average interest-earning asset denominator. If the latter is missing, omit derived NIM.

For profit bridges, keep signs consistent and reconcile beginning profit, component changes and ending profit. Distinguish operating expenses, impairment and tax. Explain any material residual rather than forcing reconciliation.

Book equity, regulatory capital and capital adequacy ratios are different measures. Book equity/assets is not the regulatory leverage ratio. Use published LCR/NSFR only when available, with scope and currency specified. Liquid-assets ratios and loan-to-deposit ratios do not substitute for LCR.

Only discuss a regulatory threshold or breach when its effective date, applicability and definition are verified from an official CBA source. Otherwise describe the observed value and trend without a compliance conclusion.

### 4.5 Economic interpretation

Credit growth exceeding real sector growth is a signal to examine. It is not proof of over-lending: one measure may be a nominal stock and the other a real flow, with different coverage. Explain inflation, starting penetration, seasonality and sector mapping limitations.

Keep CBA reserves distinct from total strategic foreign-exchange reserves. Keep merchandise trade distinct from the balance of payments/current account. Do not attribute reserve changes solely to trade, intervention or oil revenues without supporting evidence.

Deposit mix or FX-share changes do not by themselves establish depositor motives, funding stability, deposit concentration or unhedged borrower exposure. Label hypotheses accordingly.

Do not generate numerical default forecasts, causal claims, crisis probabilities or automatic lending recommendations from correlations. Scenario modules can be added later with explicit assumptions and suitable data.

## 5. Monthly deck: 18-slide blueprint

Use the following stable slide IDs and order. The normal deck has 18 main slides including the cover, plus 2–4 concise appendices. If an entire analytical block lacks usable data, omit or merge it and record the reason. Do not fabricate inputs or insert repetitive empty slides just to reach a slide count. Keep report numbering consecutive while retaining stable internal slide IDs.

Every analytical slide needs a clear question, appropriate visual, exact period/units, source footer, and one or two supported interpretations. Put detailed methodology and source-document references in speaker notes. A factual topic title is acceptable when the evidence does not support a stronger conclusion.

### M01. Cover and information cutoff

- Title: Azerbaijan Macro & Banking Monitor.
- Identify the reporting edition, generation date and information cutoff.
- State the latest macro and banking reference periods separately where they differ.
- Label the report Draft for review until the user chooses otherwise.
- Keep the cover simple. Do not add decorative stock imagery.

### M02. Executive findings

- Question: What deserves management attention this month?
- Present up to five material findings ranked by importance, each linked to an underlying analytical slide and source-backed metric.
- Cover both adverse and favourable developments where the evidence supports them.
- Each item states the observation, its possible relevance to banking and any major uncertainty.
- Show a compact evidence table or selected headline figures. Avoid repeating a generic economic summary.
- Distinguish new developments from revisions and developments already discussed last month.

### M03. Macro and banking scorecard

- Question: Where are conditions improving, weakening or unchanged?
- Use an editable table with about 10–12 key indicators: non-oil real growth, CPI inflation, wages or real wages, lending growth, deposit growth, deposit FX share, an asset-quality indicator, an indicative pricing spread, sector profit and available capital/liquidity measures.
- Columns: latest value, comparable prior value, change, reference period and trend.
- Use a small sparkline or chart only where it improves readability.
- Show unavailable data explicitly. Colour does not automatically equate faster credit growth with improvement.
- Published values must carry their actual dates. Freshness and preliminary status should be visible when material.

### M04. Economic growth and its composition

- Questions: Is growth driven by oil or non-oil activity? Is non-oil activity strengthening?
- Plot published total, oil/gas and non-oil/gas real growth with consistent comparison windows.
- Add a composition chart or official growth-contribution chart when defensible inputs exist.
- Identify large component changes and base effects. Explain YTD versus single-period measures.
- Banking relevance: credit demand, cash generation and sectors to examine more closely.

### M05. Inflation, income and household purchasing power

- Questions: Are household incomes keeping pace with prices? Is nominal spending supported by real purchasing power?
- Plot clearly labelled CPI measures and nominal/real wage or income trends for matching periods.
- Include food/non-food/services inflation where published and relevant.
- Use retail volume growth as a supporting indicator if available.
- Avoid using aggregate real-wage growth as a direct estimate of loan affordability or debt-service capacity.

### M06. Sector activity and momentum

- Question: Which economic sectors are strengthening or weakening?
- Show a heatmap or ranked bars for agriculture, industry/non-oil industry, construction or investment, trade, transport, tourism and other consistently available sectors.
- Distinguish an output measure from investment, visitor counts or a revenue proxy. Do not label investment as construction output or arrivals as tourism value added.
- Show current and previous comparable growth measures and period labels.
- Discuss the largest changes. Treat changes in YTD growth as changes in cumulative growth, not isolated monthly momentum.
- Select one sector for further investigation when the evidence is material.

### M07. External position and FX conditions

- Questions: How are exports, imports and reserves evolving? What could matter for domestic funding and FX demand?
- Show compatible export/import flows and trade balance, separating non-oil exports where published.
- Show CBA reserves separately, and FX operations or other CBA measures when verified.
- Use small aligned charts rather than combining stocks and flows on an ambiguous axis.
- Clearly identify data lags and the boundary between observed co-movement and a causal explanation.

### M08. Lending growth and composition

- Questions: How fast is lending growing, and which segments account for it?
- Show the compatible loan stock and YoY growth trend.
- Add a contribution waterfall or stacked change chart for mutually exclusive sectors or counterparty groups.
- Include new lending separately where available and helpful.
- Distinguish banks from all credit institutions. Keep mortgages, consumer lending and household lending from overlapping in a supposed total.
- Discuss concentration of growth by published segment, not borrower concentration.

### M09. Sector credit versus sector activity

- Question: Where does credit expansion differ most from the corresponding economic activity?
- Use a scatter chart or paired bars for reliably mapped sectors. Show sector loan share as an additional column or bubble size.
- Label nominal credit-stock growth and real activity growth explicitly if these are the available measures.
- Maintain a documented CBA-to-SSC sector mapping, with coverage notes and exclusions. Do not force ambiguous matches.
- Identify divergences and reasonable alternative explanations. Suggest questions for sector specialists rather than automatic credit-limit changes.

### M10. Asset quality and denominator effects

- Questions: Are problem-loan balances changing? Does portfolio growth explain changes in the reported ratio?
- Show the published asset-quality balance and its compatible ratio with the exact source label.
- Add the numerator/denominator decomposition when valid inputs exist.
- Include segment or currency detail only if published on a compatible basis.
- Make clear which borrower-level information would be needed to establish causes.

### M11. Deposit growth and funding sources

- Questions: What drives deposit growth? Which depositor categories account for the change?
- Show total compatible deposits and separate household/corporate trends where available.
- Use contributions or absolute changes to identify sources of funding growth.
- If demand/term detail is available, describe how it changes the composition of funding.
- Avoid interpreting aggregate corporate-deposit growth as proof of concentration in a particular customer.

### M12. Funding mix and dollarisation

- Questions: Are deposits moving between currencies or maturities? Is the loan currency mix moving differently?
- Show deposit FX share and household FX share where available, with percentage-point changes.
- Show compatible lending FX share separately.
- Include term/demand shares where data allows a readable second panel.
- State whether figures are at current exchange rates. Do not label FX-share changes as exchange-rate-adjusted flows unless calculated with adequate data.
- Discuss potential currency-funding questions without claiming an open FX position or unhedged borrower exposure.

### M13. Lending rates, deposit rates and pricing spread

- Questions: How is new-business pricing changing? Is the indicative loan/deposit spread widening or narrowing?
- Prioritise new lending and new term-deposit rates, split by AZN and FX where available.
- Keep rates on outstanding balances separate or place them in an appendix.
- Match maturities and populations as closely as possible and disclose residual differences.
- Label the difference as an indicative pricing spread, not NIM.
- State that changing product/customer composition can alter aggregate rates even without like-for-like repricing.

### M14. Credit growth, funding growth and maturity structure

- Questions: Is deposit growth keeping pace with lending? How are observable funding and maturity ratios changing?
- Show compatible lending and deposit growth, their absolute changes, and loan-to-deposit ratio where meaningful.
- Include published maturity distributions in separate aligned panels if definitions permit.
- Do not infer a full liquidity gap, repricing gap or structural funding deficit from partial maturity buckets.
- Identify the extra internal ALM data required to evaluate bank-specific implications.

### M15. Banking-sector profitability and efficiency

- Questions: What drives changes in sector earnings? Are earnings trends supported by underlying income and costs?
- Use verified sector P&L measures, identifying YTD or monthly amounts.
- Build a YoY profit bridge where interest income/expense, fees, operating costs, impairment and tax data reconcile.
- Show published ROA, ROE, cost-to-income or NIM only when available with definitions. Derive them only under the rules above.
- Explain annualization and any denominator estimate in notes.
- If detailed P&L data is unavailable, use a narrower published-profit trend and explain the analytical limit.

### M16. Capital and published liquidity indicators

- Questions: How are the sector's published capital and liquidity measures evolving?
- Show verified regulatory capital ratios and published liquidity indicators, retaining dates and institutional scope.
- Add book equity or liquid-asset composition only with accurate labels.
- Show regulatory thresholds only when their applicability is verified. Otherwise use a neutral trend chart.
- An aggregate sector ratio does not establish compliance or resilience of every bank.
- If only book balance-sheet measures are available, title and describe the slide accordingly.

### M17. Regional lending and deposits

- Questions: Where are lending and deposit growth occurring? Which regional changes warrant investigation?
- Use ranked bars or a compact regional table. A map is optional and must use verified boundaries and definitions.
- Separate Baku and the rest of Azerbaijan where the official classification allows it.
- Use growth, shares and compatible loan/deposit measures. Per-capita measures require population for the appropriate geography and period.
- Explain classification changes and the possible difference between booking location and the location of economic activity.
- Do not infer underserved or over-indebted populations from an aggregate regional ratio alone.

### M18. Management questions and next releases

- Convert the strongest supported findings into three management discussion questions.
- For each, identify the public signal, why it might matter, the relevant internal information needed and the next observation to watch.
- Include the next expected releases when an official calendar supports them. Distinguish an expected date from a confirmed publication.
- Suggested functions may include Treasury/ALM, Business, Credit Risk or Finance. Do not assign named individuals or claim that an action has been approved.
- Keep recommendations proportionate: investigate, compare, monitor or review assumptions. Do not assert that ATB should change a limit without its own data and governance context.

### Default appendices

- A01: Metric definitions, formulas and institutional/period coverage.
- A02: Source and freshness register, including missing indicators.
- A03: Revisions since the prior report and methodological breaks, when material.
- A04: Additional time series, balance-sheet detail or sector/regional tables used in the analysis, when useful.

## 6. Weekly digest and sector-review variants

### Weekly digest

Target 4–6 slides, combining the cover into the first slide:

1. New releases and the main developments since the last successful digest.
2. New macroeconomic evidence.
3. New banking/funding evidence.
4. The most material sector or pricing development, if one exists.
5. Questions for management and upcoming publications.
6. Sources and caveats, only if they need a separate slide.

Include only blocks supported by new observations or material revisions. A newly downloaded copy of unchanged data is not new evidence. If no meaningful data has been published, save a no-update status and keep the previous deck available. Do not force a deck of repeated findings.

### Sector review

Support an optional 8–10-slide report for a configured sector such as agriculture, construction, trade or tourism. Include sector scale, activity trends, prices/cost indicators when available, investment, external trade if relevant, credit growth, credit-versus-activity comparison, regional patterns, banking questions and sources.

Run a sector review after a relevant material release or on explicit request. Select indicators based on the sector's economics and available data. Do not apply a generic score to incompatible sector measures or claim borrower profitability from aggregate output.

## 7. Narrative generation and fact grounding

Generate a structured fact pack before producing commentary. It must include validated observations, computed metrics, changes, revisions, definitions, chart specifications and source evidence IDs.

Separate narrative generation from calculation and rendering. Implement a simple narrator interface with:

- A deterministic facts-only fallback that still produces a complete, clearly labelled descriptive report.
- A structured narrative input/output contract that Claude Code can populate during a development or cloud-routine run.
- An optional API-backed implementation, enabled only with configured credentials. Do not make a paid API call a prerequisite for building and testing data collection or producing a facts-only report.

Keep the model/provider configurable. Check current official documentation before implementing provider-specific calls. Do not hardcode an assumed latest model or embed subscription credentials in code.

For each finding, require:

- Finding ID and target slide ID.
- Statement and underlying metric/source IDs.
- Classification: observed fact, interpretation, hypothesis or management question.
- Reporting period and comparison basis.
- Material caveat or alternative explanation where relevant.

Require output in a validated structured format. Insert displayed numeric values from the fact pack, or match every number in generated text to an approved metric. Reject unsupported numbers, nonexistent sources, altered periods or falsely asserted causes. Fall back to descriptive text if validation fails.

Use previous-report findings to avoid repetitive commentary and to revisit earlier questions. A previous generated statement is not evidence for a current factual claim.

The writing should be direct and suitable for senior bankers. Avoid generic optimism, alarming language without evidence, and filler such as “the sector remains resilient” unless a precise, bounded observation supports it. Use a supported takeaway title when helpful and an accurate topic title otherwise.

Monitoring flags must be transparent and reproducible. Configure the comparison window, direction, materiality threshold and minimum observations for each flag. If using historical percentiles or unusual-movement checks, compare consistent frequency/basis and explain sensitivity to seasonality and structural breaks. A flag is a prompt for investigation, not a calibrated risk score or forecast. A large movement should trigger a source check, not automatic deletion of a potentially valid observation.

## 8. Charts, presentation design and exports

Use 16:9 slides with a restrained financial-reporting theme: white or very light background, dark navy text, a limited accent palette and consistent chart conventions. Keep a dedicated colour mapping for series across every report. Use colour plus labels rather than colour alone to communicate direction.

The principal evidence should occupy most of an analytical slide. Prefer one main visual and one or two short interpretations. Use two panels only when the comparison needs them. Move dense detail to appendices.

Aim for approximately 28–32 pt titles, 18–20 pt commentary, readable chart labels and at least 10–11 pt source notes. Adjust to the content without shrinking dense paragraphs into unreadable text. Support Azerbaijani characters in the chosen fonts.

Use editable native charts, text and tables wherever possible. A Node.js/TypeScript renderer using a suitable presentation library such as PptxGenJS is a reasonable implementation choice; check its official documentation and current capabilities. Use supported equivalent tooling if the environment has a preferred renderer. Prefer a small set of reusable slide layouts to hand-positioning every report.

PptxGenJS documentation entry point: https://gitbrent.github.io/PptxGenJS/

Do not use generated imagery for data charts or recreate charts as decorative images. If a specialised chart cannot be rendered natively, use a clearly documented vector/image fallback and provide its source data in the workbook. Do not claim such a fallback is an editable native chart.

Chart rules:

- Show units, currency, scale, reference periods and comparison basis.
- Keep series colours and semantic meaning stable.
- Use sensible axes; bar charts normally start at zero. Explain any necessary truncation.
- Prefer separate aligned charts to a misleading dual axis.
- Do not interpolate missing observations as actual data.
- Separate preliminary/revised observations where material.
- Do not draw a smooth monthly path through quarterly-only data.
- Limit labels and legends so they remain readable.
- Match all chart data exactly to the supporting workbook and narrative facts.

Sources: put a concise source label and data period on each analytical slide. Put the document URL, title, table/page/sheet reference, retrieval date, metric IDs and material methodology in the speaker notes. Citations must identify the source document or table, not just the institution homepage when a more precise reference exists.

Required monthly outputs:

- Editable `.pptx`.
- `.pdf` rendered from the same deck.
- `.xlsx` with an index, metric definitions, source register, observations, calculated indicators, exact chart data, revision log and quality summary. Use understandable labels and formulas where useful; no external workbook dependencies.
- Structured fact pack and narrative JSON.
- Machine-readable run manifest and a concise quality report.
- Slide previews for inspection.

Use an edition month plus a distinct generation timestamp/version in filenames. Preserve prior editions and revisions. Update a `latest` pointer only after a successful validated run.

## 9. Automation, release detection and scheduling

Separate source checks from report generation. The daily check should be inexpensive and should not call an LLM when nothing has changed.

Use official release calendars as expectations, with observed publication content as the actual trigger. Handle late releases and calendar changes without inventing a new observation.

For each source check:

1. Discover relevant current and historical document links.
2. Compare content/version metadata with the saved state.
3. Download changed files, extract data and validate it.
4. Identify new periods, revisions and non-data changes separately.
5. Save the validated dataset snapshot and change set.
6. Decide which report type, if any, requires generation.

Initial monthly publication policy:

- Define the reporting anchor as a banking month for which compatible loan and deposit data is available.
- Require verified banking anchors and verified SSC GDP and CPI observations to generate the main combined report. SSC observations may have different dates; show those dates.
- Wait up to seven calendar days after detecting a new banking anchor for configured companion banking tables, such as the bank overview, to arrive. Make this grace period configurable.
- If optional companion data remains unavailable, generate a labelled partial edition after the grace period, using explicitly dated prior-period values only where meaningful.
- If critical anchors are missing or invalid, save a blocked status with the cause and retain the last successful deck.
- A material correction or newly available companion block may generate a versioned revision. Avoid multiple trivial revisions of the same edition.
- The weekly digest can report new macro releases while the monthly combined edition is waiting for the banking anchor.

The grace period applies to subsequent scheduled editions. For the initial build or an explicit on-demand report, generate immediately from the latest validated snapshot, with missing optional blocks and different reporting periods disclosed. Do not delay the first demonstration for a future release.

Define materiality in configuration by series type, not through a single arbitrary universal threshold. New reporting periods are meaningful updates for the relevant report. Revision thresholds should reflect units and precision, and be documented as workflow settings rather than regulatory risk limits.

Provide a scheduler-neutral command to check sources and generate due reports. Document one practical unattended deployment path, with optional wrappers for an OS scheduler, a hosted runner, n8n or Claude Code cloud routines. Choose one configured execution path rather than implementing all of them unnecessarily.

If using Claude Code cloud routines, check the current official routines documentation, configure access to the official source/download domains, and explicitly provide persistent state and output storage. Verify required runtime packages and PDF rendering support. Do not rely on an interactive terminal staying open.

Claude Code routines documentation entry point: https://code.claude.com/docs/en/routines

Use a job lock, idempotent processing, bounded retries, structured logs and configurable runtime limits. Preserve the last successful outputs on failure. Distinguish collection failure, parser failure, data-quality failure and narrative/rendering failure. A failed fetch is not “no new data.”

Prepare and verify the local pipeline and deployment instructions first. Do not claim unattended automation is active until a real persistent runner and schedule have been configured and tested in the user's environment. Do not enable external delivery without a configured authorized destination.

## 10. Implementation structure and commands

Inspect the existing repository and applicable instructions first. Preserve unrelated files. If the project is empty, create a small maintainable repository with modules for discovery, ingestion, normalization, storage, calculations, narrative, rendering and scheduling.

Recommended configuration files:

- `sources.yaml`: discovery pages, included datasets, parser settings and source priorities.
- `metrics.yaml`: formulas, units, dimensions, comparison rules and dependencies.
- `reports.yaml`: slide definitions, critical inputs, optional modules and reporting rules.
- `settings.yaml`: timezone, language, output locations, historical window and scheduling defaults.
- Theme and terminology files.
- `.env.example`: names of optional secrets, without values.

Provide an equivalent of the following CLI contract. Exact command names may follow the existing project conventions, but all behaviours must exist:

```text
monitor discover
monitor backfill --start 2020-01
monitor refresh
monitor validate
monitor fact-pack --report monthly --as-of YYYY-MM-DD
monitor report --type monthly --as-of YYYY-MM-DD --facts-only
monitor report --type monthly --as-of YYYY-MM-DD --narrative-file narrative.json
monitor report --type weekly --since YYYY-MM-DD
monitor report --type sector --sector agriculture
monitor run-due
```

`as-of` must have an explicit definition. Use a supplied date as an information cutoff in Asia/Baku. Only claim genuine historical as-of reporting where archived publications/vintages support it; otherwise label a reconstruction accurately.

Keep numerical transformations independent of prompts and presentation layouts. Keep slide selection and key metrics configurable. Store source-extraction fixtures so parser changes can be tested against known official examples.

Secure handling should be straightforward: secrets in environment or a managed secret store, no secrets in logs or source control, no macro execution from downloaded workbooks, and source text treated as data rather than executable instructions. Public sources are sufficient for version one.

## 11. Verification and acceptance criteria

Use focused tests that cover actual reporting failure risks. Avoid creating a large test suite that merely mirrors implementation.

Required checks:

- A representative successful extraction from both CBA and SSC.
- Numeric-format normalization and source missing-value markers.
- Period parsing, especially month-end dates versus calendar-month labels and mixed cutoffs within one table.
- Correct handling of January resets when deriving monthly flows.
- Stock/flow, nominal/real and population incompatibilities rejected or visibly flagged.
- Source totals and components reconciled within stated rounding tolerances.
- Growth contributions and ratio decompositions reconcile.
- Overdue-loan labels remain distinct from NPL/Stage 3 labels.
- Ratios and pricing spreads use the intended definitions.
- A replacement file with the same URL creates a revision vintage.
- Re-running unchanged inputs produces no duplicate observations or new report edition.
- A failed download or parser does not overwrite the prior successful report.
- A fresh unattended runner can restore state, run once and save its outputs.
- All displayed numbers agree across fact pack, narrative, charts and workbook.
- Narrative contains no unsupported numerical claim or fabricated source.

For the first report, manually reconcile at least ten headline indicators against source cells/tables. Include lending, deposits, an interest-rate series, GDP and CPI, plus available banking indicators. Record actual differences and resolution rather than just “passed.”

Render the deck to PDF/previews and inspect every slide for clipped content, text overflow, unreadable labels, missing characters, broken charts and source footnotes. Inspect cover, scorecard, a dense chart and any complex bridge at normal presentation size. Fix material layout problems and re-render affected slides.

If PDF conversion or visual inspection is unavailable, deliver the verified outputs that can be produced, describe the exact remaining step and do not claim that the PDF or visual review succeeded. A file existing on disk does not establish that its rendered contents are correct.

Acceptance of the first version requires:

1. A verified source register and indicator availability matrix.
2. A persistent historical dataset with original-document provenance.
3. A reproducible snapshot and a complete first monthly report using real data.
4. Editable PPTX, supporting workbook and PDF where the rendering environment supports it.
5. Evidence of the focused calculation/extraction checks and visual review.
6. A repeat run that correctly recognises unchanged inputs.
7. A documented, runnable scheduling/deployment path and explicit deployment status.

## 12. Execution order and final handoff

Proceed in this order without asking the user to settle routine technical choices:

1. Inspect the project, create a brief implementation plan, and verify live source discovery.
2. Build the source register, availability matrix and initial metric dictionary. Identify critical gaps early.
3. Implement and demonstrate a narrow complete path using CBA lending/deposits and SSC GDP/CPI.
4. Extend collection and calculations to the remaining available slide inputs.
5. Generate the monthly fact pack, grounded narrative, editable deck and supporting workbook.
6. Validate calculations and render/inspect the deck.
7. Add weekly/sector selection and the idempotent daily-check workflow.
8. Provide the configured deployment instructions and test the chosen unattended runner when access exists.

If an API key or hosting account is unavailable, finish every component that can run locally, including the facts-only report. Request only the specific credential or deployment choice that remains necessary after the working result is available for review.

At handoff, state:

- What works and which reports were actually generated.
- Where the deliverables and source code are located.
- The exact data cutoff, reporting periods and material unavailable inputs.
- How to refresh data and generate a report with one command.
- How to change the language, template, metrics and schedule.
- Whether automation and external delivery are configured or still require deployment.
- Any observed usage cost for an enabled API, keeping measured cost separate from estimates.

Start by verifying the sources and building the first working data-to-deck path. Continue until the implemented outputs and verification evidence are reviewable.
