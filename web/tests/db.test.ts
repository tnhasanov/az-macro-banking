/**
 * The projection queries, against a real Postgres.
 *
 * Skipped unless AZMONITOR_TEST_DATABASE_URL is set, in the same way the Python cloud tests are.
 *
 * The case these exist for: the scorecard ignored the `dims` its own definition carried, so the
 * tile labelled "Indicative pricing spread, AZN" showed the FX slice — 4.09 where the AZN figure
 * was 11.50. The number was real and the label was real and they belonged to different series.
 * Nothing in a type system catches that, and nobody reading the page would have known.
 */
import test from "node:test";
import assert from "node:assert/strict";
import postgres from "postgres";

const URL = process.env.AZMONITOR_TEST_DATABASE_URL;
const skip = URL ? false : "set AZMONITOR_TEST_DATABASE_URL to run the database tests";

/**
 * A database of its own, created once and dropped at the end.
 *
 * A separate database rather than a schema: the data-access module builds its own connection from
 * an environment variable, and steering it with a `search_path` option in the URL is the kind of
 * indirection that makes a test pass or fail for reasons unrelated to what it is testing. Here it
 * did exactly that — the projection tables from the Python suite were sitting in `public` and the
 * queries found those instead.
 */
const DB_NAME = `azmonitor_webtest_${process.pid}`;

function withDatabaseName(url: string, name: string): string {
  const u = new global.URL(url);
  u.pathname = `/${name}`;
  return u.toString();
}

function adminUrl(): string {
  return withDatabaseName(URL!, "postgres");
}

async function createDatabase(): Promise<void> {
  const admin = postgres(adminUrl(), { prepare: false, max: 1 });
  try {
    await admin.unsafe(`DROP DATABASE IF EXISTS ${DB_NAME}`);
    await admin.unsafe(`CREATE DATABASE ${DB_NAME}`);
  } finally {
    await admin.end();
  }

  const db = postgres(withDatabaseName(URL!, DB_NAME), { prepare: false, max: 1 });
  try {
    await db.unsafe(`
      CREATE TABLE meta (key TEXT PRIMARY KEY, value JSONB NOT NULL,
                         updated_at TIMESTAMPTZ NOT NULL DEFAULT now());
      CREATE TABLE indicators (
        series_id TEXT NOT NULL, dims JSONB NOT NULL DEFAULT '{}'::jsonb, period_end DATE NOT NULL,
        value DOUBLE PRECISION, unit TEXT, label TEXT, kind TEXT NOT NULL, period_type TEXT,
        frequency TEXT, scenario TEXT, vintage TEXT, source_id TEXT, dataset_id TEXT,
        publication_id TEXT, published_at DATE, citation TEXT,
        origin TEXT NOT NULL DEFAULT 'source', formula TEXT);
    `);

    // Two currency slices of one series, with clearly different values, plus one undimensioned
    // series as a control.
    for (let i = 0; i < 14; i += 1) {
      const period = new Date(Date.UTC(2026, 7 - i, 0)).toISOString().slice(0, 10);
      const rows: [string, Record<string, string>, number][] = [
        ["cba.rates.new.spread", { currency: "AZN" }, 11.5 - i * 0.1],
        ["cba.rates.new.spread", { currency: "FX" }, 4.09 - i * 0.2],
        ["cba.loans.total_ci.yoy", {}, 12.88 - i * 0.3],
      ];
      for (const [id, dims, value] of rows) {
        // `json()` through the tagged template, for the same reason as the definitions row above:
        // a pre-stringified parameter is encoded twice and lands as a JSON string, which `@>`
        // then never matches — the dimension filter would appear broken when the data is.
        await db`
          INSERT INTO indicators
            (series_id, dims, period_end, value, unit, label, kind, period_type, origin, source_id)
          VALUES (${id}, ${db.json(dims)}, ${period}, ${value}, 'pp', 'Spread', 'observation',
                  'month_end_stock_growth_yoy', 'computed', 'cba')`;
      }
    }

    // Through the tagged template with `json()`, not `unsafe` with a stringified parameter: that
    // route encodes the string *again* and stores a JSON string scalar rather than an object, so
    // the row reads back as text and every field on it is undefined.
    await db`INSERT INTO meta (key, value) VALUES ('definitions', ${db.json({
        scorecard: [
          { key: "spread_azn", label: "Indicative pricing spread, AZN (new business)",
            series_id: "cba.rates.new.spread", dims: { currency: "AZN" },
            change: "lag12", change_kind: "pp", good: "neutral" },
          { key: "spread_unspecified", label: "Spread, no dimension given",
            series_id: "cba.rates.new.spread", dims: {},
            change: "lag12", change_kind: "pp", good: "neutral" },
          { key: "loans_yoy", label: "Loans to the economy, y/y",
            series_id: "cba.loans.total_ci.yoy", dims: {},
            change: "lag1", change_kind: "pp", good: "neutral" },
        ],
        sectors: [], chart_window_months: 36, report_titles: {}, status_label: null,
      })})`;
  } finally {
    await db.end();
  }
}

async function dropDatabase(): Promise<void> {
  const admin = postgres(adminUrl(), { prepare: false, max: 1 });
  try {
    await admin.unsafe(
      `SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = '${DB_NAME}'`);
    await admin.unsafe(`DROP DATABASE IF EXISTS ${DB_NAME}`);
  } finally {
    await admin.end();
  }
}

let mod: typeof import("../lib/db.ts");

test("set up the scratch database", { skip }, async () => {
  await createDatabase();
  process.env.AZMONITOR_DATABASE_URL = withDatabaseName(URL!, DB_NAME);
  mod = await import("../lib/db.ts");
  assert.equal(await mod.isPopulated(), true);
  const defs = await mod.definitions();
  assert.ok(defs, "the definitions row was not written");
  assert.equal(defs.scorecard.length, 3);
});

test("the scorecard shows the dimension its definition asks for", { skip }, async () => {
  {
    const db = mod;
    const cells = await db.scorecard();
    const azn = cells.find((c) => c.row.key === "spread_azn");
    assert.ok(azn, "the AZN row is missing");
    // 11.5 is the AZN leg. 4.09 is the FX leg, and is what the bug showed here.
    assert.equal(azn.current?.value, 11.5);
    assert.notEqual(azn.current?.value, 4.09);
    // Its comparison must come from the same slice, not from whichever row sorted into place.
    // `lag12` takes rn 13, which is i = 12 in the fixture: 11.5 - 1.2 = 10.3 on the AZN leg.
    // The FX leg at that period is 4.09 - 2.4 = 1.69, so a mix-up here would be unmistakable.
    assert.ok(azn.previous);
    assert.equal(Math.round((azn.previous.value ?? 0) * 100) / 100, 10.3);
  }
});

test("a dimensioned series with no dimension given shows nothing, and says why", { skip }, async () => {
  {
    const db = mod;
    const cells = await db.scorecard();
    const ambiguous = cells.find((c) => c.row.key === "spread_unspecified");
    assert.ok(ambiguous);
    assert.equal(ambiguous.current, null, "an arbitrary slice was shown");
    assert.match(ambiguous.unavailable ?? "", /more than one dimension/);
  }
});

test("an undimensioned series is unaffected", { skip }, async () => {
  {
    const db = mod;
    const cells = await db.scorecard();
    const loans = cells.find((c) => c.row.key === "loans_yoy");
    assert.ok(loans);
    assert.equal(loans.current?.value, 12.88);
    assert.equal(loans.unavailable, undefined);
  }
});

test("a reporting period comes back as a plain date, not a shifted instant", { skip }, async () => {
  {
    const db = mod;
    const s = await db.series("cba.loans.total_ci.yoy", "observation", 3);
    assert.ok(s);
    for (const point of s.points) {
      assert.equal(typeof point.period_end, "string",
        "a DATE parsed into a Date object can slip a day, and so a month, west of UTC");
      assert.match(point.period_end, /^\d{4}-\d{2}-\d{2}$/);
    }
    // Ascending, so a chart draws left to right.
    const periods = s.points.map((p) => p.period_end);
    assert.deepEqual([...periods].sort(), periods);
  }
});

test("tear down the scratch database", { skip }, async () => {
  await dropDatabase();
});
