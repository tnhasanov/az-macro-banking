/**
 * The request rules, on the same cases the Python tests use (tests/fixtures/request-cases.json).
 * The browser form, this API and the worker must agree on what a valid request is; if either
 * implementation drifts, one side's test fails.
 */
import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { InvalidRequest, normalise, SECTORS } from "../lib/params.ts";

const CASES = JSON.parse(readFileSync(new URL("../../tests/fixtures/request-cases.json", import.meta.url), "utf8"));

test("the web application knows the configured sectors", () => {
  assert.deepEqual([...SECTORS].sort(), [...CASES.sectors].sort());
});

for (const c of CASES.cases) {
  test(`${c.report} ${JSON.stringify(c.raw)}`, () => {
    if (c.ok) {
      assert.deepEqual(normalise(c.report, c.raw, SECTORS), c.ok);
    } else {
      assert.throws(() => normalise(c.report, c.raw, SECTORS),
        (e: unknown) => e instanceof InvalidRequest && e.code === c.error);
    }
  });
}
