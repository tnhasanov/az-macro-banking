/**
 * Banking analytics.
 *
 * The panels are built from validated metric definitions only. Where a series is not in the
 * dataset, its panel is simply absent — an empty chart with a plausible title is worse than no
 * chart, because it invites the reader to assume the number is zero rather than unknown.
 */
import {
  definitions, forecastVintages, isPopulated, latestForCategories, series, seriesMany, stressPaths,
} from "@/lib/db";
import { AnalyticsPanels, ProjectionPanel, type PanelSpec } from "@/components/Panels";
import { Empty } from "@/components/ui";

export const dynamic = "force-dynamic";

/** Validated metric ids, grouped as the deck groups them. Nothing here is invented for the screen. */
const PANELS: { id: string; title: string; note?: string; unit: string | null;
  kind: "lines" | "level-rate"; ids: string[]; zeroLine?: boolean }[] = [
  {
    id: "credit", title: "Credit to the economy", kind: "level-rate", unit: null,
    note: "All credit institutions, nominal stock at current exchange rates.",
    ids: ["cba.loans.total_ci", "cba.loans.total_ci.yoy"],
  },
  {
    id: "deposits", title: "Deposits", kind: "level-rate", unit: null,
    note: "All credit institutions, all currencies.",
    ids: ["cba.deposits.total", "cba.deposits.total.yoy"],
  },
  {
    id: "dollarisation", title: "Dollarisation", kind: "lines", unit: "%",
    note: "FX share of loans and of deposits, AZN equivalent at current rates.",
    ids: ["cba.loans.fx_share", "cba.deposits.fx_share"],
  },
  {
    id: "asset-quality", title: "Asset quality", kind: "lines", unit: "%",
    note: "Two different measures on two different populations — not interchangeable.",
    ids: ["cba.bank.npl.ratio", "cba.loans.overdue_ratio"],
  },
  {
    id: "funding", title: "Loan-to-deposit ratio", kind: "lines", unit: "%",
    note: "Gross loans over total deposits, all credit institutions. Not a regulatory liquidity ratio.",
    ids: ["cba.ldr"],
  },
  {
    id: "profitability", title: "Profitability, annualised", kind: "lines", unit: "%",
    note: "Year-to-date profit annualised over average month-end balances.",
    ids: ["cba.bank.roa_annualised", "cba.bank.roe_annualised"],
  },
  {
    id: "capital", title: "Capital and liquidity", kind: "lines", unit: "%",
    note: "Book ratios from the published balance sheet, not regulatory capital measures.",
    ids: ["cba.bank.equity_to_assets", "cba.bank.liquid_assets_ratio"],
  },
  {
    id: "efficiency", title: "Cost-to-income", kind: "lines", unit: "%",
    note: "Non-interest expense over net interest plus non-interest income, year-to-date.",
    ids: ["cba.bank.pnl.cost_to_income"],
  },
  {
    id: "portfolio", title: "Bank loan portfolio growth", kind: "lines", unit: "%",
    note: "Business, consumer and mortgage lending, year on year.",
    ids: ["cba.bank.portfolio.business.yoy", "cba.bank.portfolio.consumer.yoy",
      "cba.bank.portfolio.mortgage.yoy"],
    zeroLine: true,
  },
];

/** The series a projection panel would draw, if the dataset holds any. */
const PROJECTED = ["cba.loans.total_ci.yoy", "ssc.cpi.all.yoy"];

export default async function AnalyticsPage() {
  if (!(await isPopulated())) {
    return (
      <>
        <header className="topbar"><h1>Banking analytics</h1></header>
        <div className="content">
          <Empty title="No data has been published to this dashboard yet." />
        </div>
      </>
    );
  }

  const defs = await definitions();
  const months = 120;

  const built = await Promise.all(
    PANELS.map(async (p): Promise<PanelSpec | null> => {
      const found = await seriesMany(p.ids, "observation", months);
      // A level-and-rate panel needs both halves; one alone would be a different chart.
      if (found.length === 0 || (p.kind === "level-rate" && found.length < 2)) return null;
      return {
        id: p.id, title: p.title, note: p.note, unit: p.unit, kind: p.kind,
        zeroLine: p.zeroLine,
        // Kept in the order the panel declares, so the first series is always the one colour 1 is
        // assigned to regardless of what the database returned first.
        series: p.ids
          .map((id) => found.find((s) => s.series_id === id))
          .filter((s): s is (typeof found)[number] => s !== undefined),
      };
    }),
  );
  const panels = built.filter((p): p is PanelSpec => p !== null);

  const sectors = await latestForCategories(
    (defs?.sectors ?? [])
      .filter((s) => s.credit_series)
      .map((s) => ({ key: s.key, label: s.label, series_id: `${s.credit_series}.yoy` })),
  );

  const projections = await Promise.all(
    PROJECTED.map(async (id) => {
      const [history, vintages, stress] = await Promise.all([
        series(id, "observation", 60), forecastVintages(id), stressPaths(id),
      ]);
      if (vintages.length === 0 && stress.length === 0) return null;
      return { id, history, vintages, stress, unit: history?.unit ?? "%",
        title: history?.label ?? id };
    }),
  );
  const withProjections = projections.filter((p) => p !== null);

  return (
    <>
      <header className="topbar">
        <div>
          <h1>Banking analytics</h1>
          <p>
            Series published by the Central Bank and the State Statistical Committee, plus the
            metrics this system derives from them. Derived figures say so, with the formula, so the
            arithmetic here is never read as the publisher&apos;s.
          </p>
        </div>
      </header>

      <div className="content">
        {panels.length === 0 ? (
          <Empty title="None of the analytics series are in the current dataset.">
            The engine publishes them after a successful refresh.
          </Empty>
        ) : (
          <AnalyticsPanels
            panels={panels}
            sectors={sectors}
            sectorNote="Loans to each sector, year on year, from the Central Bank sector table."
            initialMonths={defs?.chart_window_months ?? 36}
          />
        )}

        {withProjections.map((p) => (
          <ProjectionPanel
            key={p!.id}
            title={`${p!.title} — observed and projected`}
            note="Projections are shown apart from the history they follow."
            history={p!.history}
            vintages={p!.vintages}
            stress={p!.stress}
            unit={p!.unit}
          />
        ))}

        {withProjections.length === 0 && (
          <div className="notice">
            <strong>No forecasts or stress results are in the dataset.</strong> When the engine
            stores them, they appear here as separate dashed paths — one line per forecast round and
            one per stress scenario — never joined to the observed series.
          </div>
        )}
      </div>
    </>
  );
}
