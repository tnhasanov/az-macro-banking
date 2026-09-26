/**
 * A fresh database for one test file, with the application-state migrations applied through the
 * web application's own `ensureSchema` — so these tests also prove the TypeScript side applies the
 * same migrations the worker does.
 */
import postgres from "postgres";

export const TEST_URL = process.env.AZMONITOR_TEST_DATABASE_URL;
export const skip = TEST_URL ? false : "set AZMONITOR_TEST_DATABASE_URL to run the database tests";

export async function freshDatabase(tag: string): Promise<{ url: string; drop: () => Promise<void> }> {
  const name = `azmonitor_${tag}_${process.pid}`;
  const admin = postgres(TEST_URL!, { max: 1, onnotice: () => undefined });
  await admin.unsafe(`DROP DATABASE IF EXISTS ${name}`);
  await admin.unsafe(`CREATE DATABASE ${name}`);
  const u = new URL(TEST_URL!);
  u.pathname = `/${name}`;
  process.env.AZMONITOR_DATABASE_URL = u.toString();
  return {
    url: u.toString(),
    async drop() {
      const { sql } = await import("../../lib/db.ts");
      await sql().end({ timeout: 2 });
      await admin.unsafe(`DROP DATABASE IF EXISTS ${name} WITH (FORCE)`);
      await admin.end();
    },
  };
}

/** A published edition, as the worker's publication transaction would have written it. */
export async function publish(sql: postgres.Sql, opts: { editionId?: string; reportType?: string; edition?: string;
  version?: number; cause?: string; pdfBytes?: number; jobId?: string | null } = {}) {
  const reportType = opts.reportType ?? "monthly";
  const edition = opts.edition ?? "2026-07";
  const version = opts.version ?? 1;
  const editionId = opts.editionId ?? `${reportType}:${edition}:v${version}`;
  const prefix = `reports/${reportType}/${edition}/v${version}/job_x-a1`;
  await sql`INSERT INTO publication_records(edition_id, report_type, edition, version, environment, job_id, cause,
                fingerprint, manifest, validation, reporting_periods, information_cutoff, findings, limitations)
            VALUES (${editionId}, ${reportType}, ${edition}, ${version}, 'production', ${opts.jobId ?? null}, ${opts.cause ?? "new_data"},
                    'fp1', ${sql.json({ prefix, files: [
                      { name: "deck.pdf", key: `${prefix}/deck.pdf`, bytes: opts.pdfBytes ?? 1000, sha256: "abc", role: "pdf", content_type: "application/pdf" },
                      { name: "deck.pptx", key: `${prefix}/deck.pptx`, bytes: 2000, sha256: "def", role: "pptx", content_type: "x" },
                    ] })},
                    ${sql.json({ checks: [{ id: "pdf", ok: true, detail: "1 page" }] })},
                    ${sql.json({ banking_period: "2026-07-31", cpi_period: "2026-08-31" })}, '2026-09-26T19:59:59Z',
                    ${sql.json(["Loans to households grew 18.2% y/y.", "The NPL ratio was 2.9%."])},
                    ${sql.json(["Inputs cover different periods."])})`;
  return editionId;
}
