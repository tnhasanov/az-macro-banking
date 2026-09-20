/**
 * Publication monitoring.
 *
 * What the sources have released, and what this system did about it. Three dates are kept apart
 * deliberately: when the publisher released it, when the English translation appeared, and when
 * this system first saw it. Collapsing them would make a late download look like a late release.
 */
import { isPopulated, publications } from "@/lib/db";
import { Badge, Card, Empty } from "@/components/ui";
import { dateLong, period, sinceNow } from "@/lib/format";

export const dynamic = "force-dynamic";

export default async function PublicationsPage() {
  if (!(await isPopulated())) {
    return (
      <>
        <header className="topbar"><h1>Publications</h1></header>
        <div className="content">
          <Empty title="No publications have been recorded yet." />
        </div>
      </>
    );
  }

  const pubs = await publications(120);
  const unprocessed = pubs.filter((p) => !p.processed);
  const awaitingReport = pubs.filter((p) => p.processed && !p.report_generated);

  return (
    <>
      <header className="topbar">
        <div>
          <h1>Publications</h1>
          <p>
            Every release the monitor has detected from the Central Bank and the State Statistical
            Committee, with the date it was published, the date an English version became available
            and the date this system first saw it.
          </p>
        </div>
      </header>

      <div className="content">
        <div className="grid grid-4">
          <Card>
            <div className="stat">
              <span className="stat-label">Detected releases</span>
              <span className="stat-value">{pubs.length}</span>
              <div className="stat-prov">Across all monitored source types.</div>
            </div>
          </Card>
          <Card>
            <div className="stat">
              <span className="stat-label">Awaiting extraction</span>
              <span className="stat-value">{unprocessed.length}</span>
              <div className="stat-prov">
                Detected but not yet parsed into observations or passages.
              </div>
            </div>
          </Card>
          <Card>
            <div className="stat">
              <span className="stat-label">Extracted, no report yet</span>
              <span className="stat-value">{awaitingReport.length}</span>
              <div className="stat-prov">
                Readiness rules decide when a report may be produced; a release alone is not enough.
              </div>
            </div>
          </Card>
          <Card>
            <div className="stat">
              <span className="stat-label">Most recent release</span>
              <span className="stat-value" style={{ fontSize: 19 }}>
                {pubs[0] ? dateLong(pubs[0].published_at) : "—"}
              </span>
              <div className="stat-prov">
                {pubs[0] ? `${pubs[0].pub_type} · first seen ${sinceNow(pubs[0].first_seen_at)}` : "—"}
              </div>
            </div>
          </Card>
        </div>

        <Card title="Detected publications"
          note="Publication date is the source's own release date, not the date this system downloaded it.">
          <div className="table-scroll">
            <table className="data">
              <thead>
                <tr>
                  <th scope="col">Publication</th>
                  <th scope="col">Covers</th>
                  <th scope="col">Published</th>
                  <th scope="col">English</th>
                  <th scope="col">First seen</th>
                  <th scope="col">Extraction</th>
                  <th scope="col">Reported</th>
                </tr>
              </thead>
              <tbody>
                {pubs.map((p) => (
                  <tr key={p.publication_id}>
                    <td>
                      {p.title_en ?? p.edition_label ?? p.publication_id}
                      <span className="sub">
                        {p.pub_type}
                        {p.original_language && ` · original ${p.original_language.toUpperCase()}`}
                        {p.source_urls?.length > 0 && (
                          <>
                            {" · "}
                            {p.source_urls.map((u, i) => (
                              <span key={u.url}>
                                {i > 0 && " "}
                                <a href={u.url} target="_blank" rel="noreferrer noopener">
                                  {u.language?.toUpperCase() || "source"}
                                </a>
                              </span>
                            ))}
                          </>
                        )}
                      </span>
                    </td>
                    <td className="nowrap">{period(p.reporting_period_end)}</td>
                    <td className="nowrap">
                      {dateLong(p.published_at)}
                      {p.published_at_basis && <span className="sub">{p.published_at_basis}</span>}
                    </td>
                    <td className="nowrap">
                      {p.translation_available_at ? dateLong(p.translation_available_at) : "—"}
                    </td>
                    <td className="nowrap">{dateLong(p.first_seen_at)}</td>
                    <td className="nowrap">
                      {p.extraction_status
                        ? <Badge tone={p.extraction_status === "ok" ? "good" : "warning"}>
                            {p.extraction_status}
                          </Badge>
                        : <span className="muted">not attempted</span>}
                      {p.passages > 0 && (
                        <span className="sub">
                          {p.verified_passages}/{p.passages} passages verified
                        </span>
                      )}
                    </td>
                    <td className="nowrap">
                      {p.report_generated
                        ? <Badge tone="good">in a report</Badge>
                        : <Badge tone="neutral">not yet</Badge>}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Card>
      </div>
    </>
  );
}
