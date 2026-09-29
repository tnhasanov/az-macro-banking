import test from "node:test";
import assert from "node:assert/strict";
import { databaseProblem, databaseSource } from "../lib/dbproblem.ts";

const URL = "postgresql://owner:s3cret-pass@ep-example-pooler.eu-central-1.aws.neon.tech/neondb?sslmode=require";

test("no variable set names the variable and the fix", () => {
  const said = databaseProblem(new Error("No database is configured"), {});
  assert.match(said, /AZMONITOR_DATABASE_URL is not set/);
  assert.match(said, /Production/);
});

test("an empty or blank variable counts as not set", () => {
  assert.equal(databaseSource({ AZMONITOR_DATABASE_URL: "  " }), null);
});

test("each known failure is named, and nothing from the value or the driver's message leaks", () => {
  const cases: [Record<string, unknown>, RegExp][] = [
    [{ code: "ENOTFOUND", message: "getaddrinfo ENOTFOUND ep-example-pooler.eu-central-1.aws.neon.tech" }, /host .* not found/],
    [{ code: "28P01", message: 'password authentication failed for user "owner"' }, /rejected the password/],
    [{ code: "ERR_INVALID_URL", name: "TypeError", message: `Invalid URL: '${URL}'` }, /not a valid connection string/],
    [{ code: "CONNECT_TIMEOUT", message: "write CONNECT_TIMEOUT ep-example-pooler:5432" }, /timed out/],
  ];
  for (const [error, expected] of cases) {
    const said = databaseProblem(error, { AZMONITOR_DATABASE_URL: URL });
    assert.match(said, /^AZMONITOR_DATABASE_URL is set, but /);
    assert.match(said, expected);
    for (const secret of ["owner", "s3cret", "ep-example", "neon.tech", "5432"]) {
      assert.ok(!said.includes(secret), `"${secret}" must not appear in: ${said}`);
    }
  }
});

test("an unknown failure reports its name and code only", () => {
  const said = databaseProblem({ name: "PostgresError", code: "XX000", message: "internal: owner at host" },
                               { DATABASE_URL: URL });
  assert.equal(said, "DATABASE_URL is set, but connecting failed with PostgresError (XX000).");
});
