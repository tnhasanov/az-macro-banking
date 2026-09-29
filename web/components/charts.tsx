"use client";

/**
 * The charts.
 *
 * Three rules are enforced here rather than left to whoever adds the next page:
 *
 * **One axis.** Every chart takes one unit. Two measures of different scale are two charts. There
 * is no prop for a second y-axis and there should never be one.
 *
 * **Observed and projected are drawn differently.** History is a solid line; a forecast vintage or
 * a stress path is dashed, starts where the observations stop, and is labelled with the round or
 * scenario that produced it. A dotted rule marks the boundary, so the eye cannot read a projection
 * as data.
 *
 * **Identity is never colour alone.** Two or more series always get a legend, and the tooltip names
 * every series it shows. A table view sits under each chart for anyone who cannot use the colours.
 *
 * Series colours come from the validated categorical set in globals.css, assigned in fixed order
 * and never cycled: a ninth series would repeat a colour, so the callers facet instead.
 */
import { useId, useState } from "react";
import {
  Area, AreaChart, Bar, BarChart, CartesianGrid, Cell, ComposedChart, Legend, Line, LineChart,
  ReferenceLine, ResponsiveContainer, Tooltip, XAxis, YAxis,
} from "recharts";
import { dateShort, num, period as fmtPeriod, withUnit } from "@/lib/format";

export const SERIES_COLORS = [
  "var(--series-1)", "var(--series-2)", "var(--series-3)", "var(--series-4)",
];

const AXIS = { stroke: "var(--axis)", fontSize: 11, tickLine: false };
const MARGIN = { top: 8, right: 14, bottom: 4, left: 4 };

export interface ChartSeries {
  key: string;
  label: string;
  /** Solid for what was observed, dashed for anything projected. */
  projected?: boolean;
  /** Shown in the legend beside the label, e.g. "2026 Q1 round" or "severe scenario". */
  qualifier?: string;
  /** Overrides the chart's unit for this row of the table view, where two units can coexist. */
  unit?: string | null;
}

interface Row { period_end: string; [key: string]: string | number | null }

function Tip({ active, payload, label, unit, footer }: {
  active?: boolean;
  payload?: { name?: string; value?: number; color?: string; dataKey?: string }[];
  label?: string;
  unit?: string | null;
  footer?: string;
}) {
  if (!active || !payload?.length) return null;
  const rows = payload.filter((p) => p.value !== null && p.value !== undefined);
  if (!rows.length) return null;
  return (
    <div className="tooltip">
      <div className="tooltip-head">{fmtPeriod(label)}</div>
      {rows.map((p) => (
        <div className="tooltip-row" key={String(p.dataKey)}>
          <span className="name">
            <span className="legend-swatch" style={{ background: p.color }} aria-hidden />
            {p.name}
          </span>
          <span className="val">{withUnit(p.value ?? null, unit ?? null)}</span>
        </div>
      ))}
      {footer && <div className="tooltip-foot">{footer}</div>}
    </div>
  );
}

function LegendBlock({ series }: { series: ChartSeries[] }) {
  if (series.length < 2) return null;
  return (
    <div className="legend">
      {series.map((s, i) => (
        <span className="legend-item" key={s.key}>
          {s.projected ? (
            <span
              className="legend-swatch dashed"
              style={{ color: SERIES_COLORS[i % SERIES_COLORS.length] }}
              aria-hidden
            />
          ) : (
            <span
              className="legend-swatch"
              style={{ background: SERIES_COLORS[i % SERIES_COLORS.length] }}
              aria-hidden
            />
          )}
          {s.label}
          {s.qualifier && <span className="muted"> · {s.qualifier}</span>}
        </span>
      ))}
    </div>
  );
}

/** Always available, because colour is never the only way to read a chart here. */
function TableView({ rows, series, unit }: { rows: Row[]; series: ChartSeries[]; unit?: string | null }) {
  const id = useId();
  return (
    <details className="table-view">
      <summary aria-describedby={id}>View as a table</summary>
      <div className="table-scroll">
        <table className="data" id={id}>
          <thead>
            <tr>
              <th scope="col">Period</th>
              {series.map((s) => (
                <th scope="col" key={s.key} style={{ textAlign: "right" }}>
                  {s.label}{s.qualifier ? ` (${s.qualifier})` : ""}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {[...rows].reverse().map((r) => (
              <tr key={r.period_end}>
                <td className="nowrap">{fmtPeriod(r.period_end)}</td>
                {series.map((s) => (
                  <td className="num" key={s.key}>
                    {withUnit(r[s.key] as number | null, s.unit ?? unit ?? null)}
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </details>
  );
}

/**
 * A time series, optionally with projections attached.
 *
 * `boundary` is the last observed period. Everything drawn to the right of it is a projection, and
 * the rule makes that visible without reading the legend.
 */
export function TimeSeries({
  rows, series, unit, height = 240, boundary, footer, zeroLine = false,
}: {
  rows: Row[];
  series: ChartSeries[];
  unit?: string | null;
  height?: number;
  boundary?: string | null;
  footer?: string;
  zeroLine?: boolean;
}) {
  if (!rows.length) {
    return <p className="muted" style={{ fontSize: 13 }}>No readings in the current dataset.</p>;
  }
  return (
    <div className="chart-wrap">
      <LegendBlock series={series} />
      <ResponsiveContainer width="100%" height={height}>
        <LineChart data={rows} margin={MARGIN}>
          <CartesianGrid stroke="var(--grid)" vertical={false} />
          <XAxis dataKey="period_end" tickFormatter={dateShort} {...AXIS} minTickGap={28} />
          <YAxis
            {...AXIS}
            width={54}
            tickFormatter={(v: number) => num(v, Math.abs(v) >= 1000 ? 0 : 1)}
            axisLine={false}
          />
          <Tooltip
            content={<Tip unit={unit} footer={footer} />}
            cursor={{ stroke: "var(--axis)", strokeDasharray: "3 3" }}
          />
          {zeroLine && <ReferenceLine y={0} stroke="var(--border-strong)" />}
          {boundary && (
            <ReferenceLine
              x={boundary}
              stroke="var(--ink-muted)"
              strokeDasharray="2 4"
              label={{ value: "last observed", position: "insideTopRight", fill: "var(--ink-muted)", fontSize: 10 }}
            />
          )}
          {series.map((s, i) => (
            <Line
              key={s.key}
              type="monotone"
              dataKey={s.key}
              name={s.label + (s.qualifier ? ` · ${s.qualifier}` : "")}
              stroke={SERIES_COLORS[i % SERIES_COLORS.length]}
              strokeWidth={2}
              strokeDasharray={s.projected ? "5 4" : undefined}
              dot={false}
              activeDot={{ r: 4.5, strokeWidth: 2, stroke: "var(--surface-raised)" }}
              connectNulls={false}
              isAnimationActive={false}
            />
          ))}
        </LineChart>
      </ResponsiveContainer>
      <TableView rows={rows} series={series} unit={unit} />
    </div>
  );
}

/** A single series as an area, for a level that is read as a magnitude rather than a rate. */
export function AreaSeries({
  rows, series, unit, height = 200, footer,
}: { rows: Row[]; series: ChartSeries; unit?: string | null; height?: number; footer?: string }) {
  const id = useId();
  if (!rows.length) return <p className="muted" style={{ fontSize: 13 }}>No readings in the current dataset.</p>;
  return (
    <div className="chart-wrap">
      <ResponsiveContainer width="100%" height={height}>
        <AreaChart data={rows} margin={MARGIN}>
          <defs>
            <linearGradient id={id} x1="0" y1="0" x2="0" y2="1">
              <stop offset="0%" stopColor="var(--series-1)" stopOpacity={0.22} />
              <stop offset="100%" stopColor="var(--series-1)" stopOpacity={0.02} />
            </linearGradient>
          </defs>
          <CartesianGrid stroke="var(--grid)" vertical={false} />
          <XAxis dataKey="period_end" tickFormatter={dateShort} {...AXIS} minTickGap={28} />
          <YAxis {...AXIS} width={58} tickFormatter={(v: number) => num(v, 0)} axisLine={false} />
          <Tooltip content={<Tip unit={unit} footer={footer} />} cursor={{ stroke: "var(--axis)", strokeDasharray: "3 3" }} />
          <Area
            type="monotone" dataKey={series.key} name={series.label}
            stroke="var(--series-1)" strokeWidth={2} fill={`url(#${id})`}
            activeDot={{ r: 4.5, strokeWidth: 2, stroke: "var(--surface-raised)" }}
            isAnimationActive={false}
          />
        </AreaChart>
      </ResponsiveContainer>
      <TableView rows={rows} series={[series]} unit={unit} />
    </div>
  );
}

/**
 * Categories ranked by magnitude — sector shares, contributions to growth.
 *
 * One hue, because the categories are one measure and their identity is carried by the axis label,
 * not by colour. Negative values take the second hue so a drag on growth reads as one.
 */
export function CategoryBars({
  rows, unit, height, label = "Value", diverging = false,
}: {
  rows: { name: string; value: number; note?: string }[];
  unit?: string | null;
  height?: number;
  label?: string;
  diverging?: boolean;
}) {
  const h = height ?? Math.max(160, rows.length * 30 + 24);
  if (!rows.length) return <p className="muted" style={{ fontSize: 13 }}>No categories in the current dataset.</p>;
  return (
    <div className="chart-wrap">
      <ResponsiveContainer width="100%" height={h}>
        <BarChart data={rows} layout="vertical" margin={{ top: 4, right: 40, bottom: 4, left: 4 }} barCategoryGap={6}>
          <CartesianGrid stroke="var(--grid)" horizontal={false} />
          <XAxis type="number" {...AXIS} tickFormatter={(v: number) => num(v, 1)} axisLine={false} />
          <YAxis
            type="category" dataKey="name" {...AXIS} width={168} axisLine={false}
            tick={{ fill: "var(--ink-secondary)", fontSize: 12 }}
          />
          <Tooltip
            content={<Tip unit={unit} />}
            cursor={{ fill: "var(--surface-sunken)" }}
          />
          {diverging && <ReferenceLine x={0} stroke="var(--border-strong)" />}
          <Bar dataKey="value" name={label} radius={[0, 4, 4, 0]} isAnimationActive={false}>
            {rows.map((r) => (
              <Cell
                key={r.name}
                fill={diverging && r.value < 0 ? "var(--series-2)" : "var(--series-1)"}
                stroke="var(--surface-raised)"
                strokeWidth={2}
              />
            ))}
          </Bar>
        </BarChart>
      </ResponsiveContainer>
      <details className="table-view">
        <summary>View as a table</summary>
        <div className="table-scroll">
          <table className="data">
            <thead>
              <tr><th scope="col">Category</th><th scope="col" style={{ textAlign: "right" }}>{label}</th></tr>
            </thead>
            <tbody>
              {rows.map((r) => (
                <tr key={r.name}>
                  <td>{r.name}{r.note && <span className="sub">{r.note}</span>}</td>
                  <td className="num">{withUnit(r.value, unit ?? null)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </details>
    </div>
  );
}

/**
 * A level with its growth rate — as two stacked charts sharing an x-axis, never two y-axes.
 *
 * This is the shape that most often tempts a dual axis, so it exists as a component to make the
 * right answer the easy one.
 */
export function LevelAndRate({
  rows, levelKey, rateKey, levelLabel, rateLabel, levelUnit, rateUnit,
}: {
  rows: Row[];
  levelKey: string; rateKey: string;
  levelLabel: string; rateLabel: string;
  levelUnit?: string | null; rateUnit?: string | null;
}) {
  return (
    <div className="stack" style={{ gap: 6 }}>
      <div>
        <div className="card-note" style={{ marginBottom: 2 }}>{levelLabel}</div>
        <ResponsiveContainer width="100%" height={150}>
          <ComposedChart data={rows} margin={MARGIN}>
            <CartesianGrid stroke="var(--grid)" vertical={false} />
            <XAxis dataKey="period_end" tick={false} axisLine={false} height={2} />
            <YAxis {...AXIS} width={58} tickFormatter={(v: number) => num(v, 0)} axisLine={false} />
            <Tooltip content={<Tip unit={levelUnit} />} cursor={{ stroke: "var(--axis)", strokeDasharray: "3 3" }} />
            <Line type="monotone" dataKey={levelKey} name={levelLabel} stroke="var(--series-1)"
              strokeWidth={2} dot={false} isAnimationActive={false}
              activeDot={{ r: 4.5, strokeWidth: 2, stroke: "var(--surface-raised)" }} />
          </ComposedChart>
        </ResponsiveContainer>
      </div>
      <div>
        <div className="card-note" style={{ marginBottom: 2 }}>{rateLabel}</div>
        <ResponsiveContainer width="100%" height={130}>
          <ComposedChart data={rows} margin={MARGIN}>
            <CartesianGrid stroke="var(--grid)" vertical={false} />
            <XAxis dataKey="period_end" tickFormatter={dateShort} {...AXIS} minTickGap={28} />
            <YAxis {...AXIS} width={58} tickFormatter={(v: number) => num(v, 1)} axisLine={false} />
            <Tooltip content={<Tip unit={rateUnit} />} cursor={{ stroke: "var(--axis)", strokeDasharray: "3 3" }} />
            <ReferenceLine y={0} stroke="var(--border-strong)" />
            <Line type="monotone" dataKey={rateKey} name={rateLabel} stroke="var(--series-3)"
              strokeWidth={2} dot={false} isAnimationActive={false}
              activeDot={{ r: 4.5, strokeWidth: 2, stroke: "var(--surface-raised)" }} />
          </ComposedChart>
        </ResponsiveContainer>
      </div>
      {/* A level and a rate are two units, so the table carries each series' own. */}
      <TableView
        rows={rows}
        series={[
          { key: levelKey, label: levelLabel, unit: levelUnit },
          { key: rateKey, label: rateLabel, unit: rateUnit },
        ]}
      />
    </div>
  );
}

/** A range selector, so the reader picks the window rather than the page picking it for them. */
export function RangePicker({
  value, onChange, options = [24, 36, 60, 120],
}: { value: number; onChange: (v: number) => void; options?: number[] }) {
  return (
    <div className="seg" role="group" aria-label="Time range">
      {options.map((m) => (
        <button key={m} type="button" aria-pressed={value === m} onClick={() => onChange(m)}>
          {m >= 120 ? "10y" : `${m / 12}y`}
        </button>
      ))}
    </div>
  );
}

export function useRange(initial = 60) {
  return useState<number>(initial);
}

/** Several series keyed by period, so one chart can draw them on a shared x-axis. Pure. */
export function toRows(
  seriesList: { series_id: string; points: { period_end: string; value: number }[] }[],
): Row[] {
  const byPeriod = new Map<string, Row>();
  for (const s of seriesList) {
    for (const p of s.points) {
      const row = byPeriod.get(p.period_end) ?? { period_end: p.period_end };
      row[s.series_id] = p.value;
      byPeriod.set(p.period_end, row);
    }
  }
  return [...byPeriod.values()].sort((a, b) => a.period_end.localeCompare(b.period_end));
}
