/**
 * System monitoring.
 *
 * Run history, data quality, locks and delivery state. The delivery table is read-only on purpose:
 * a delivery whose outcome is uncertain is resolved by a person at the command line against the
 * real ledger, never by a button here. A dashboard that could mark a send as done would be able to
 * lose an email.
 */
import {
  deliveries, isPopulated, jobRuns, lastRunPerTask, locks, meta, qualityChecks,
} from "@/lib/db";
import { Badge, Card, Empty, StatusBadge } from "@/components/ui";
import { bakuTime, dateLong, sinceNow } from "@/lib/format";

export const dynamic = "force-dynamic";

/** The schedule, in the clock it is written in. Kept beside the runs so a miss is obvious. */
const SCHEDULE = [
  { task: "source-check", local: "09:15, 13:15, 17:15 Baku", utc: "05:15, 09:15, 13:15 UTC" },
  { task: "weekly-digest", local: "Monday 08:30 Baku", utc: "Monday 04:30 UTC" },
  { task: "monitor", local: "07:45, 18:45 Baku", utc: "03:45, 14:45 UTC" },
];

export default async function SystemPage() {
  if (!(await isPopulated())) {
    return (
      <>
        <header className="topbar"><h1>System</h1></header>
        <div className="content">
          <Empty title="The read model is empty.">
            No run has published to this database yet.
          </Empty>
        </div>
      </>
    );
  }

  const [runs, lastRuns, checks, sends, held, lastPublish] = await Promise.all([
    jobRuns(40), lastRunPerTask(), qualityChecks(), deliveries(40), locks().catch(() => []),
    meta<{ at: string; counts: Record<string, number>; quality: Record<string, number> }>(
      "last_readmodel_publish",
    ),
  ]);

  const failing = checks.filter((c) => !c.ok);
  const blocking = failing.filter((c) => (c.severity ?? "").toLowerCase() === "critical");
  const uncertain = sends.filter((d) => d.status === "needs_review");
  const active = held.filter((l) => !l.expired);

  return (
    <>
      <header className="topbar">
        <div>
          <h1>System</h1>
          <p>
            Scheduled runs, data quality and delivery state. All times are shown in Asia/Baku, the
            clock the schedule is written in; the scheduler itself runs in UTC.
          </p>
        </div>
      </header>

      <div className="content">
        {uncertain.length > 0 && (
          <div className="notice warn">
            <strong>
              {uncertain.length} {uncertain.length === 1 ? "delivery needs" : "deliveries need"} a
              decision.
            </strong>{" "}
            These were left in an uncertain state and are never retried automatically, because a
            retry could send a report twice. Resolve them with{" "}
            <span className="mono">python -m azmonitor.cli delivery resolve</span>.
          </div>
        )}
        {blocking.length > 0 && (
          <div className="notice bad">
            <strong>{blocking.length} blocking quality {blocking.length === 1 ? "check" : "checks"} failing.</strong>{" "}
            Report generation will not proceed while these fail.
          </div>
        )}

        <div className="grid grid-2">
          <Card title="Schedule" note="Asia/Baku is UTC+4 all year; there is no daylight saving to drift.">
            <div className="table-scroll">
              <table className="data">
                <thead>
                  <tr>
                    <th scope="col">Task</th><th scope="col">Local</th>
                    <th scope="col">Scheduler (UTC)</th><th scope="col">Last run</th>
                  </tr>
                </thead>
                <tbody>
                  {SCHEDULE.map((s) => {
                    const last = lastRuns.find((r) => r.task === s.task);
                    return (
                      <tr key={s.task}>
                        <td className="nowrap">{s.task}</td>
                        <td className="nowrap">{s.local}</td>
                        <td className="nowrap muted">{s.utc}</td>
                        <td className="nowrap">
                          {last ? (
                            <>
                              <StatusBadge status={last.status} />
                              <span className="sub">{sinceNow(last.started_at)}</span>
                            </>
                          ) : (
                            <span className="muted">never</span>
                          )}
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          </Card>

          <Card title="Read model" note="Rebuilt from the dataset after every successful run.">
            {lastPublish ? (
              <>
                <table className="data">
                  <tbody>
                    <tr><td>Last published</td><td className="num">{bakuTime(lastPublish.at)}</td></tr>
                    {Object.entries(lastPublish.counts ?? {}).map(([k, v]) => (
                      <tr key={k}><td>{k}</td><td className="num">{v.toLocaleString("en-GB")}</td></tr>
                    ))}
                  </tbody>
                </table>
                <div className="stat-prov">
                  This projection is derived and disposable. The engine&apos;s own dataset remains the
                  authority for every figure.
                </div>
              </>
            ) : (
              <p className="muted" style={{ fontSize: 13 }}>Not published yet.</p>
            )}
            {active.length > 0 && (
              <div className="notice" style={{ marginTop: 12 }}>
                <strong>A run is holding the lease.</strong>{" "}
                {active.map((l) => (
                  <span key={l.name} className="mono">
                    {l.name} — {l.holder}, until {bakuTime(l.expires_at)}
                  </span>
                ))}
              </div>
            )}
          </Card>
        </div>

        <Card title="Data quality"
          note={`${checks.length - failing.length} of ${checks.length} checks passing`}>
          {checks.length === 0 ? (
            <p className="muted" style={{ fontSize: 13 }}>No checks recorded.</p>
          ) : (
            <div className="table-scroll">
              <table className="data">
                <thead>
                  <tr>
                    <th scope="col">Check</th><th scope="col">Type</th><th scope="col">Result</th>
                    <th scope="col">Failed</th><th scope="col">Detail</th>
                  </tr>
                </thead>
                <tbody>
                  {[...checks].sort((a, b) => Number(a.ok) - Number(b.ok)).map((c) => (
                    <tr key={c.id}>
                      <td className="mono">{c.id}</td>
                      <td className="nowrap">{c.check_type ?? "—"}</td>
                      <td>
                        {c.ok
                          ? <Badge tone="good">pass</Badge>
                          : <Badge tone={c.severity === "critical" ? "critical" : "warning"}>
                              {c.severity ?? "fail"}
                            </Badge>}
                      </td>
                      <td className="num">{c.failed} / {c.comparisons}</td>
                      <td>
                        {c.message ?? "—"}
                        {c.failed_periods?.length > 0 && (
                          <span className="sub">
                            Periods: {c.failed_periods.slice(0, 6).map((p) => dateLong(p)).join(", ")}
                            {c.failed_periods.length > 6 && ` and ${c.failed_periods.length - 6} more`}
                          </span>
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </Card>

        <Card title="Run history" note="The last 40 scheduled and manual runs.">
          <div className="table-scroll">
            <table className="data">
              <thead>
                <tr>
                  <th scope="col">Started</th><th scope="col">Task</th><th scope="col">Trigger</th>
                  <th scope="col">Outcome</th><th scope="col">Produced</th><th scope="col">Notes</th>
                </tr>
              </thead>
              <tbody>
                {runs.map((r) => (
                  <tr key={r.run_id}>
                    <td className="nowrap">
                      {bakuTime(r.started_at)}
                      <span className="sub">{sinceNow(r.started_at)}</span>
                    </td>
                    <td className="nowrap">{r.task}</td>
                    <td className="nowrap muted">{r.trigger ?? "—"}</td>
                    <td><StatusBadge status={r.status} /></td>
                    <td>
                      {r.produced?.length
                        ? r.produced.map((p) => (
                            <span key={p.report_type} className="nowrap">
                              {p.report_type}: <strong>{p.status}</strong>{" "}
                            </span>
                          ))
                        : <span className="muted">none</span>}
                    </td>
                    <td>
                      {r.error
                        ? <span style={{ color: "var(--critical)" }}>{r.error}</span>
                        : Object.entries(r.readiness ?? {})
                            .filter(([, v]) => !v.ok)
                            .map(([k, v]) => (
                              <span key={k} className="sub">{k}: {v.state}{v.note ? ` — ${v.note}` : ""}</span>
                            ))}
                    </td>
                  </tr>
                ))}
                {runs.length === 0 && (
                  <tr><td colSpan={6} className="muted">No runs recorded.</td></tr>
                )}
              </tbody>
            </table>
          </div>
        </Card>

        <Card title="Deliveries"
          note="A mirror of the engine's ledger. Read-only: resolving one is a command-line action.">
          <div className="table-scroll">
            <table className="data">
              <thead>
                <tr>
                  <th scope="col">Report</th><th scope="col">Channel</th><th scope="col">Recipient</th>
                  <th scope="col">Outcome</th><th scope="col">Attempts</th><th scope="col">When</th>
                </tr>
              </thead>
              <tbody>
                {sends.map((d) => (
                  <tr key={d.delivery_id}>
                    <td>
                      {d.report_type} {d.edition}
                      {d.version !== null && <span className="muted"> v{d.version}</span>}
                      {d.subject && <span className="sub">{d.subject}</span>}
                    </td>
                    <td className="nowrap">{d.channel}</td>
                    <td className="nowrap">{d.recipient_hint ?? d.recipient_id}</td>
                    <td>
                      <StatusBadge status={d.status} />
                      {d.last_error && <span className="sub">{d.last_error}</span>}
                    </td>
                    <td className="num">{d.attempts}</td>
                    <td className="nowrap">{bakuTime(d.sent_at ?? d.updated_at)}</td>
                  </tr>
                ))}
                {sends.length === 0 && (
                  <tr>
                    <td colSpan={6} className="muted">
                      No delivery has been attempted. Outbound delivery is disabled in this
                      deployment.
                    </td>
                  </tr>
                )}
              </tbody>
            </table>
          </div>
        </Card>
      </div>
    </>
  );
}
