/**
 * The pieces every page is built from.
 *
 * `StatTile` is the important one: it will not render a figure without a period and an attribution,
 * because the whole system exists to stop a number appearing on a screen with neither.
 */
import type { ReactNode } from "react";
import { provenance, statusTone, withUnit, type Tone } from "@/lib/format";
import type { SeriesMeta } from "@/lib/db";

export function Card({
  title, note, action, children, className = "",
}: {
  title?: ReactNode; note?: ReactNode; action?: ReactNode; children: ReactNode; className?: string;
}) {
  return (
    <section className={`card ${className}`}>
      {(title || note || action) && (
        <header className="card-head">
          <div>
            {title && <h3 className="card-title">{title}</h3>}
            {note && <div className="card-note">{note}</div>}
          </div>
          {action}
        </header>
      )}
      {children}
    </section>
  );
}

export function Badge({ tone = "neutral", children }: { tone?: Tone; children: ReactNode }) {
  return <span className={`badge badge-${tone}`}>{children}</span>;
}

export function StatusBadge({ status }: { status: string | null | undefined }) {
  return <Badge tone={statusTone(status)}>{status ?? "unknown"}</Badge>;
}

/** The two or three lines that make a figure readable. Never optional. */
export function Provenance({ meta }: { meta: Partial<SeriesMeta> & { period_end?: string | null } }) {
  return (
    <div className="stat-prov">
      {provenance(meta).map((line, i) => (
        <div className="prov-line" key={i}>{line}</div>
      ))}
    </div>
  );
}

export function StatTile({
  label, value, unit, delta, deltaLabel, meta, missing,
}: {
  label: string;
  value: number | null | undefined;
  unit?: string | null;
  delta?: number | null;
  deltaLabel?: string;
  meta?: (Partial<SeriesMeta> & { period_end?: string | null }) | null;
  missing?: string;
}) {
  if (value === null || value === undefined || !meta) {
    return (
      <Card>
        <div className="stat">
          <span className="stat-label">{label}</span>
          <span className="stat-value muted">—</span>
          <div className="stat-prov">
            {missing ?? "Not available in the current dataset. Nothing is shown rather than a stale figure."}
          </div>
        </div>
      </Card>
    );
  }
  const tone: Tone = delta === null || delta === undefined ? "neutral" : delta > 0 ? "good" : delta < 0 ? "serious" : "neutral";
  return (
    <Card>
      <div className="stat">
        <span className="stat-label">{label}</span>
        <span className="stat-value">{withUnit(value, unit ?? meta.unit)}</span>
        {delta !== null && delta !== undefined && (
          <span className="stat-delta" style={{ color: `var(--${tone === "neutral" ? "ink-muted" : tone})` }}>
            {delta > 0 ? "▲" : delta < 0 ? "▼" : "▪"} {withUnit(Math.abs(delta), "pp")} {deltaLabel ?? "y/y"}
          </span>
        )}
        <Provenance meta={meta} />
      </div>
    </Card>
  );
}

export function Empty({ title, children }: { title: string; children?: ReactNode }) {
  return (
    <div className="notice">
      <strong>{title}</strong>
      {children && <div style={{ marginTop: 6 }}>{children}</div>}
    </div>
  );
}

export function KindTag({ kind }: { kind: string }) {
  return <span className="kind-tag">{kind}</span>;
}
