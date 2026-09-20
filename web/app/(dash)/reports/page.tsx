/**
 * The report library.
 *
 * Every edition ever produced, newest first, each one immutable. A version number that has appeared
 * here is never reused and never overwritten, so an edition linked in an email six months ago still
 * resolves to the bytes that were sent.
 */
import Link from "next/link";
import { definitions, editions, isPopulated } from "@/lib/db";
import { Badge, Card, Empty, StatusBadge } from "@/components/ui";
import { bytes, dateLong, period, reportTitle } from "@/lib/format";

export const dynamic = "force-dynamic";

export default async function ReportsPage({
  searchParams,
}: { searchParams: Promise<{ type?: string }> }) {
  const { type } = await searchParams;
  if (!(await isPopulated())) {
    return (
      <>
        <header className="topbar"><h1>Reports</h1></header>
        <div className="content">
          <Empty title="No reports have been published to this dashboard yet.">
            The engine uploads each edition after a successful run.
          </Empty>
        </div>
      </>
    );
  }

  const [all, everything, defs] = await Promise.all([editions(type), editions(), definitions()]);
  const types = [...new Set(everything.map((e) => e.report_type))].sort();
  const titles = defs?.report_titles;

  return (
    <>
      <header className="topbar">
        <div>
          <h1>Reports</h1>
          <p>
            Every edition, with the reporting period it covers and the version that was produced.
            Editions are immutable: a new version is added, never substituted.
          </p>
        </div>
      </header>

      <div className="content">
        <div className="controls">
          <Link href="/reports" className="button" aria-current={!type ? "page" : undefined}>
            All types
          </Link>
          {types.map((t) => (
            <Link key={t} href={`/reports?type=${encodeURIComponent(t)}`} className="button"
              aria-current={type === t ? "page" : undefined}>
              {reportTitle(t, titles)}
            </Link>
          ))}
        </div>

        {all.length === 0 ? (
          <Empty title="No editions of this type." />
        ) : (
          <div className="stack">
            {all.map((e) => (
              <Card key={`${e.report_type}/${e.edition}/${e.version}`}>
                <div className="row" style={{ alignItems: "flex-start" }}>
                  <div style={{ minWidth: 0, flex: "1 1 320px" }}>
                    <h3 style={{ fontSize: 15 }}>
                      <Link href={`/reports/${e.report_type}/${e.edition}/${e.version}`}>
                        {reportTitle(e.report_type, titles)} — {e.edition}
                      </Link>
                    </h3>
                    <div className="prov-line" style={{ marginTop: 3 }}>
                      Version {e.version} · produced {dateLong(e.generated_at)}
                      {e.as_of && ` · data as of ${period(e.as_of)}`}
                      {e.n_slides ? ` · ${e.n_slides} slides` : ""}
                    </div>
                    {Object.keys(e.reporting_periods ?? {}).length > 0 && (
                      <div className="prov-line">
                        Reporting periods:{" "}
                        {Object.entries(e.reporting_periods).map(([k, v], i) => (
                          <span key={k}>{i > 0 && " · "}{k} {period(v)}</span>
                        ))}
                      </div>
                    )}
                  </div>
                  <div className="row" style={{ gap: 6, flex: "0 0 auto" }}>
                    {e.partial && <Badge tone="warning">Partial edition</Badge>}
                    {e.narrative_mode && <Badge tone="neutral">{e.narrative_mode}</Badge>}
                    <StatusBadge status={e.status_label} />
                  </div>
                </div>

                {e.summary?.length > 0 && (
                  <ul style={{ margin: "10px 0 0", paddingLeft: 18, fontSize: 13 }}>
                    {e.summary.slice(0, 3).map((line, i) => <li key={i}>{line}</li>)}
                  </ul>
                )}

                <div className="row" style={{ marginTop: 12, gap: 8 }}>
                  {(e.files ?? []).map((f) => (
                    <a key={f.key} className="button" href={`/api/files/${f.key}`}>
                      {f.name.split(".").pop()?.toUpperCase()}
                      <span className="muted" style={{ fontWeight: 400 }}>{bytes(f.bytes)}</span>
                    </a>
                  ))}
                  {(e.files ?? []).length === 0 && (
                    <span className="muted" style={{ fontSize: 12.5 }}>
                      No files were uploaded for this edition.
                    </span>
                  )}
                </div>
              </Card>
            ))}
          </div>
        )}
      </div>
    </>
  );
}
