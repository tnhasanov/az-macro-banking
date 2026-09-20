/**
 * One edition.
 *
 * Shows what the edition actually claims: the findings its own narrative validator passed, the
 * fingerprint that decided it was worth publishing, the reporting period of every input, and the
 * quality result at the time it was produced. Nothing here is recomputed — a report is a record of
 * what was true when it was made, and re-deriving it now would quietly rewrite history.
 */
import Link from "next/link";
import { notFound } from "next/navigation";
import { definitions, edition } from "@/lib/db";
import { Badge, Card, StatusBadge } from "@/components/ui";
import { bytes, dateLong, period, reportTitle } from "@/lib/format";

export const dynamic = "force-dynamic";

export default async function EditionPage({
  params,
}: { params: Promise<{ type: string; edition: string; version: string }> }) {
  const p = await params;
  const version = Number.parseInt(p.version, 10);
  if (!Number.isFinite(version)) notFound();
  const [e, defs] = await Promise.all([
    edition(decodeURIComponent(p.type), decodeURIComponent(p.edition), version),
    definitions(),
  ]);
  if (!e) notFound();

  const quality = Object.entries(e.quality ?? {});

  return (
    <>
      <header className="topbar">
        <div>
          <div className="prov-line"><Link href="/reports">← All reports</Link></div>
          <h1>{reportTitle(e.report_type, defs?.report_titles)} — {e.edition}</h1>
          <p>
            Version {e.version}, produced {dateLong(e.generated_at)}
            {e.as_of && `, data as of ${period(e.as_of)}`}. This edition is immutable.
          </p>
        </div>
        <div className="row" style={{ gap: 6 }}>
          {e.partial && <Badge tone="warning">Partial edition</Badge>}
          <StatusBadge status={e.status_label} />
        </div>
      </header>

      <div className="content">
        <Card title="Files">
          <div className="row" style={{ gap: 8 }}>
            {(e.files ?? []).map((f) => (
              <a key={f.key} className="button" href={`/api/files/${f.key}`}>
                {f.name} <span className="muted" style={{ fontWeight: 400 }}>{bytes(f.bytes)}</span>
              </a>
            ))}
            {(e.files ?? []).length === 0 && (
              <span className="muted" style={{ fontSize: 13 }}>No files recorded for this edition.</span>
            )}
          </div>
          <div className="stat-prov">
            Downloads are served through this dashboard and require a signed-in session. The
            underlying storage is not publicly readable.
          </div>
        </Card>

        {e.summary?.length > 0 && (
          <Card title="Findings"
            note="From this edition's own narrative, after claim-level validation. Nothing is added here.">
            <ul style={{ margin: "6px 0 0", paddingLeft: 18, fontSize: 13.5, lineHeight: 1.6 }}>
              {e.summary.map((line, i) => <li key={i}>{line}</li>)}
            </ul>
          </Card>
        )}

        <div className="grid grid-2">
          <Card title="Reporting periods"
            note="These inputs are published on different cycles and are not aligned.">
            {Object.keys(e.reporting_periods ?? {}).length === 0 ? (
              <p className="muted" style={{ fontSize: 13 }}>Not recorded for this edition.</p>
            ) : (
              <table className="data">
                <tbody>
                  {Object.entries(e.reporting_periods).map(([k, v]) => (
                    <tr key={k}>
                      <td>{k}</td>
                      <td className="num">{period(v)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </Card>

          <Card title="How this edition was decided">
            <table className="data">
              <tbody>
                <tr><td>Trigger</td><td className="num">{e.trigger ?? "—"}</td></tr>
                <tr>
                  <td>Edition fingerprint</td>
                  <td className="num mono" style={{ wordBreak: "break-all" }}>
                    {e.fingerprint ? e.fingerprint.slice(0, 16) + "…" : "—"}
                  </td>
                </tr>
                <tr><td>Narrative mode</td><td className="num">{e.narrative_mode ?? "—"}</td></tr>
                <tr>
                  <td>Numbers checked against sources</td>
                  <td className="num">{e.numbers_checked ?? "—"}</td>
                </tr>
              </tbody>
            </table>
            <div className="stat-prov">
              The fingerprint covers the inputs, the narrative and the configuration. An identical
              fingerprint means no new edition is produced.
            </div>
          </Card>
        </div>

        {quality.length > 0 && (
          <Card title="Quality at the time of production">
            <div className="row" style={{ gap: 8 }}>
              {quality.map(([k, v]) => (
                <Badge key={k} tone={k === "failed" && Number(v) > 0 ? "warning" : k === "critical" && Number(v) > 0 ? "critical" : "neutral"}>
                  {k}: {String(v)}
                </Badge>
              ))}
            </div>
          </Card>
        )}
      </div>
    </>
  );
}
