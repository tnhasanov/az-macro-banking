/**
 * Overview.
 *
 * The scorecard is the one the monthly deck renders, read from the definitions the worker
 * publishes, so the screen and the deck cannot drift apart. Every tile carries its own reporting
 * period, because these series do not share one: GDP is year-to-date and quarterly, CPI is monthly,
 * the banking aggregates are a month behind the macro ones. Printing them under a single "as of"
 * date would be the most convincing way to be wrong.
 */
import Link from "next/link";
import {
  definitions, isPopulated, latestEditions, lastRunPerTask, publications, qualityChecks, scorecard,
} from "@/lib/db";
import { Badge, Card, Empty, StatusBadge } from "@/components/ui";
import {
  bakuTime, dateLong, period, provenance, reportTitle, signed, sinceNow, withUnit,
} from "@/lib/format";

export const dynamic = "force-dynamic";
export const revalidate = 0;

export default async function OverviewPage() {
  if (!(await isPopulated())) return <NotYetRun />;

  const [defs, cards, editions, runs, checks, pubs] = await Promise.all([
    definitions(), scorecard(), latestEditions(), lastRunPerTask(), qualityChecks(), publications(6),
  ]);

  const failing = checks.filter((c) => !c.ok);
  const blocking = failing.filter((c) => (c.severity ?? "").toLowerCase() === "critical");
  const periods = [...new Set(cards.map((c) => c.current?.period_end).filter(Boolean))].sort();

  return (
    <>
      <header className="topbar">
        <div>
          <h1>Overview</h1>
          <p>
            {defs?.status_label ? `${defs.status_label}. ` : ""}
            Each figure below is shown for its own reporting period — these series are not published
            on the same cycle and are never aligned to a single date.
            {periods.length > 1 && (
              <> Periods here span {period(periods[0])} to {period(periods[periods.length - 1])}.</>
            )}
          </p>
        </div>
      </header>

      <div className="content">
        {blocking.length > 0 && (
          <div className="notice bad">
            <strong>{blocking.length} blocking quality {blocking.length === 1 ? "check is" : "checks are"} failing.</strong>{" "}
            Report generation is blocked until these are resolved.{" "}
            <Link href="/system">See the checks</Link>.
          </div>
        )}

        <section>
          <h2 style={{ marginBottom: 10 }}>Scorecard</h2>
          <div className="grid grid-4">
            {cards.length === 0 && (
              <Empty title="No scorecard definitions have been published yet.">
                The worker writes these on its first successful run.
              </Empty>
            )}
            {cards.map(({ row, current, previous, unavailable }) => (
              <ScoreTile key={row.key} label={row.label} current={current} previous={previous}
                good={row.good} changeKind={row.change_kind} change={row.change}
                unavailable={unavailable} />
            ))}
          </div>
        </section>

        <div className="grid grid-2">
          <Card title="Latest report of each type"
            action={<Link href="/reports" className="card-note">All reports →</Link>}>
            {editions.length === 0 ? (
              <p className="muted" style={{ fontSize: 13 }}>No report has been produced yet.</p>
            ) : (
              <div className="table-scroll">
                <table className="data">
                  <thead>
                    <tr>
                      <th scope="col">Report</th><th scope="col">Edition</th>
                      <th scope="col">Produced</th><th scope="col">State</th>
                    </tr>
                  </thead>
                  <tbody>
                    {editions.map((e) => (
                      <tr key={`${e.report_type}/${e.edition}/${e.version}`}>
                        <td>
                          <Link href={`/reports/${e.report_type}/${e.edition}/${e.version}`}>
                            {reportTitle(e.report_type, defs?.report_titles)}
                          </Link>
                          {e.partial && <span className="sub">Partial edition</span>}
                        </td>
                        <td className="nowrap">{e.edition} <span className="muted">v{e.version}</span></td>
                        <td className="nowrap">{dateLong(e.generated_at)}</td>
                        <td><StatusBadge status={e.status_label} /></td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </Card>

          <Card title="Scheduled runs"
            action={<Link href="/system" className="card-note">System →</Link>}>
            {runs.length === 0 ? (
              <p className="muted" style={{ fontSize: 13 }}>No run has been recorded yet.</p>
            ) : (
              <div className="table-scroll">
                <table className="data">
                  <thead>
                    <tr>
                      <th scope="col">Task</th><th scope="col">Last run</th>
                      <th scope="col">Outcome</th><th scope="col">Produced</th>
                    </tr>
                  </thead>
                  <tbody>
                    {runs.map((r) => (
                      <tr key={r.task}>
                        <td className="nowrap">{r.task}</td>
                        <td className="nowrap">
                          {sinceNow(r.started_at)}
                          <span className="sub">{bakuTime(r.started_at)}</span>
                        </td>
                        <td><StatusBadge status={r.status} /></td>
                        <td className="num">{r.produced?.length ?? 0}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </Card>
        </div>

        <div className="grid grid-2">
          <Card title="Data quality"
            note={`${checks.length - failing.length} of ${checks.length} checks passing`}>
            {failing.length === 0 ? (
              <p style={{ fontSize: 13, margin: 0 }}>
                <Badge tone="good">All checks pass</Badge>{" "}
                <span className="muted">Every reconciliation in the dictionary agrees.</span>
              </p>
            ) : (
              <ul style={{ margin: 0, paddingLeft: 18, fontSize: 13 }}>
                {failing.slice(0, 6).map((c) => (
                  <li key={c.id} style={{ marginBottom: 4 }}>
                    <Badge tone={c.severity === "critical" ? "critical" : "warning"}>{c.severity ?? "issue"}</Badge>{" "}
                    <span className="mono">{c.id}</span>
                    <span className="sub">{c.message ?? `${c.failed} of ${c.comparisons} comparisons failed`}</span>
                  </li>
                ))}
              </ul>
            )}
          </Card>

          <Card title="Latest source publications"
            action={<Link href="/publications" className="card-note">Monitoring →</Link>}>
            {pubs.length === 0 ? (
              <p className="muted" style={{ fontSize: 13 }}>Nothing detected yet.</p>
            ) : (
              <ul style={{ margin: 0, paddingLeft: 18, fontSize: 13 }}>
                {pubs.map((p) => (
                  <li key={p.publication_id} style={{ marginBottom: 5 }}>
                    {p.title_en ?? p.edition_label ?? p.publication_id}
                    <span className="sub">
                      {p.pub_type} · published {dateLong(p.published_at)}
                      {p.reporting_period_end && ` · covers ${period(p.reporting_period_end)}`}
                    </span>
                  </li>
                ))}
              </ul>
            )}
          </Card>
        </div>
      </div>
    </>
  );
}

function ScoreTile({
  label, current, previous, good, changeKind, change, unavailable,
}: {
  label: string;
  current: Awaited<ReturnType<typeof scorecard>>[number]["current"];
  previous: { value: number } | null;
  good: "up" | "down" | "neutral";
  changeKind: string | null;
  change: string | null;
  unavailable?: string;
}) {
  if (!current) {
    return (
      <Card>
        <div className="stat">
          <span className="stat-label">{label}</span>
          <span className="stat-value muted">—</span>
          <div className="stat-prov">
            {unavailable
              ?? "Not in the current dataset. No figure is shown rather than an out-of-date one."}
          </div>
        </div>
      </Card>
    );
  }
  const delta = previous ? current.value - previous.value : null;
  const pct = changeKind === "pct" && previous && previous.value !== 0
    ? ((current.value / previous.value) - 1) * 100
    : null;
  const shown = pct ?? delta;
  // A change that rounds to zero at the precision shown reads as "−0.0", which looks like a
  // decline that is not there. Say what it is instead.
  const negligible = shown !== null && Math.abs(shown) < 0.05;
  const tone = shown === null || negligible || good === "neutral" ? "muted"
    : (shown > 0) === (good === "up") ? "good" : "serious";
  const windowLabel = change === "lag12" ? "y/y" : change === "lag1" ? "m/m" : "";

  return (
    <Card>
      <div className="stat">
        <span className="stat-label">{label}</span>
        <span className="stat-value">{withUnit(current.value, current.unit)}</span>
        {shown !== null && (
          <span className="stat-delta" style={{ color: `var(--${tone === "muted" ? "ink-muted" : tone})` }}>
            {negligible ? (
              <>▪ little changed {windowLabel}</>
            ) : (
              <>
                {shown > 0 ? "▲" : "▼"} {signed(shown, 1)}
                {pct !== null ? "%" : changeKind === "pp" ? " pp" : ""} {windowLabel}
              </>
            )}
          </span>
        )}
        <div className="stat-prov">
          {provenance(current).map((line, i) => <div className="prov-line" key={i}>{line}</div>)}
        </div>
      </div>
    </Card>
  );
}

function NotYetRun() {
  return (
    <>
      <header className="topbar"><h1>Overview</h1></header>
      <div className="content">
        <Empty title="The dashboard has no data yet.">
          <p style={{ margin: "6px 0" }}>
            The read model is empty, which means the reporting engine has not completed a run against
            this database. The dashboard shows nothing at all rather than placeholder figures.
          </p>
          <p style={{ margin: "6px 0 0" }} className="muted">
            Run the <span className="mono">scheduled</span> workflow once, or check{" "}
            <Link href="/system">System</Link> for the last attempt.
          </p>
        </Empty>
      </div>
    </>
  );
}
