"use client";

/**
 * The analytics panels.
 *
 * Data arrives already fetched from the server, so changing the window is instant and no query runs
 * on a keypress. The range picker trims a series that is already in hand rather than asking for a
 * different one — which also means the reader can never see two panels drawn from two different
 * fetches of a moving dataset.
 */
import { useState } from "react";
import { CategoryBars, LevelAndRate, RangePicker, TimeSeries, toRows } from "@/components/charts";
import { Card } from "@/components/ui";
import type { Series, SeriesMeta, Point } from "@/lib/db";
import { dateLong, period, sourceName } from "@/lib/format";

function trim(s: Series, months: number): Series {
  return { ...s, points: s.points.slice(-months) };
}

/** The caption under every panel. Same three facts as a stat tile, for a set of series. */
function PanelProvenance({ series }: { series: Series[] }) {
  const computed = series.filter((s) => s.origin === "computed");
  const sources = [...new Set(series.map((s) => sourceName(s.source_id, s.series_id)))];
  const latest = series
    .map((s) => s.latest_period)
    .filter((p): p is string => Boolean(p))
    .sort();
  return (
    <div className="stat-prov">
      <div className="prov-line">
        Observed data · latest reading {period(latest[latest.length - 1])}
        {latest.length > 1 && latest[0] !== latest[latest.length - 1] && (
          <> · earliest of these series ends {period(latest[0])}</>
        )}
      </div>
      <div className="prov-line">Source: {sources.join("; ")}</div>
      {computed.length > 0 && (
        <div className="prov-line">
          Computed here: {computed.map((s) => `${s.label ?? s.series_id} (${s.formula ?? "derived"})`).join("; ")}
        </div>
      )}
    </div>
  );
}

export interface PanelSpec {
  id: string;
  title: string;
  note?: string;
  unit: string | null;
  kind: "lines" | "level-rate";
  series: Series[];
  zeroLine?: boolean;
}

export function AnalyticsPanels({
  panels, sectors, sectorNote, initialMonths,
}: {
  panels: PanelSpec[];
  sectors: { key: string; label: string; value: number; meta: SeriesMeta & Point }[];
  sectorNote: string;
  initialMonths: number;
}) {
  const [months, setMonths] = useState(initialMonths);

  return (
    <>
      <div className="controls" style={{ marginBottom: 2 }}>
        <span className="card-note">Window</span>
        <RangePicker value={months} onChange={setMonths} />
        <span className="spacer" />
        <span className="card-note">
          Every panel shows observed data only. Forecasts and stress paths are drawn separately and
          are never continued from a history line.
        </span>
      </div>

      <div className="grid grid-2">
        {panels.map((p) => {
          const trimmed = p.series.map((s) => trim(s, months));
          return (
            <Card key={p.id} title={p.title} note={p.note}>
              {p.kind === "level-rate" && trimmed.length === 2 ? (
                <LevelAndRate
                  rows={toRows(trimmed)}
                  levelKey={trimmed[0].series_id}
                  rateKey={trimmed[1].series_id}
                  levelLabel={trimmed[0].label ?? trimmed[0].series_id}
                  rateLabel={trimmed[1].label ?? trimmed[1].series_id}
                  levelUnit={trimmed[0].unit}
                  rateUnit={trimmed[1].unit}
                />
              ) : (
                <TimeSeries
                  rows={toRows(trimmed)}
                  series={trimmed.map((s) => ({
                    key: s.series_id,
                    label: s.label ?? s.series_id,
                  }))}
                  unit={p.unit}
                  zeroLine={p.zeroLine}
                />
              )}
              <PanelProvenance series={trimmed} />
            </Card>
          );
        })}
      </div>

      {sectors.length > 0 && (
        <Card title="Credit growth by sector" note={sectorNote}>
          <CategoryBars
            rows={[...sectors]
              .sort((a, b) => b.value - a.value)
              .map((s) => ({ name: s.label, value: s.value }))}
            unit="%"
            label="Year-on-year growth"
            diverging
          />
          <div className="stat-prov">
            <div className="prov-line">
              Observed · {period(sectors[0]?.meta.period_end, sectors[0]?.meta.period_type)}
              {sectors.some((s) => s.meta.period_end !== sectors[0].meta.period_end) && (
                <> — sectors with a different period are labelled in the table view</>
              )}
            </div>
            <div className="prov-line">
              Computed here (year-on-year growth) from {sourceName(sectors[0]?.meta.source_id)}
              {sectors[0]?.meta.published_at && `, published ${dateLong(sectors[0].meta.published_at)}`}
            </div>
          </div>
        </Card>
      )}
    </>
  );
}

/**
 * Forecast rounds and stress paths, drawn against history but never joined to it.
 *
 * Each vintage is a whole line of its own. Blending two rounds into one path would produce a
 * forecast nobody ever made.
 */
export function ProjectionPanel({
  history, vintages, stress, unit, title, note,
}: {
  history: Series | null;
  vintages: { vintage: string; points: { period_end: string; value: number }[] }[];
  stress: { scenario: string; period_end: string; value: number }[];
  unit: string | null;
  title: string;
  note?: string;
}) {
  const byScenario = new Map<string, { period_end: string; value: number }[]>();
  for (const s of stress) {
    if (!byScenario.has(s.scenario)) byScenario.set(s.scenario, []);
    byScenario.get(s.scenario)!.push({ period_end: s.period_end, value: s.value });
  }

  const lines = [
    ...(history ? [{ series_id: "observed", points: history.points }] : []),
    ...vintages.map((v) => ({ series_id: `forecast:${v.vintage}`, points: v.points })),
    ...[...byScenario.entries()].map(([name, pts]) => ({ series_id: `stress:${name}`, points: pts })),
  ];

  const spec = [
    ...(history ? [{ key: "observed", label: history.label ?? "Observed" }] : []),
    ...vintages.map((v) => ({
      key: `forecast:${v.vintage}`, label: "Forecast", projected: true,
      qualifier: `${v.vintage} round`,
    })),
    ...[...byScenario.keys()].map((name) => ({
      key: `stress:${name}`, label: "Stress", projected: true, qualifier: name,
    })),
  ];

  if (lines.length === 0) return null;

  return (
    <Card title={title} note={note}>
      <TimeSeries
        rows={toRows(lines)}
        series={spec}
        unit={unit}
        boundary={history?.latest_period ?? null}
        footer="Dashed lines are projections, not data."
        height={260}
      />
      <div className="stat-prov">
        <div className="prov-line">
          Solid: observed readings. Dashed: projections, each shown whole and labelled with the
          forecast round or stress scenario that produced it.
        </div>
        <div className="prov-line">
          Projections are not observations and are never extended from the history line.
        </div>
      </div>
    </Card>
  );
}
