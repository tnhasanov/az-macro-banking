/** The scheduler's occurrences, in Baku time, and the catch-up rule for a late or missed tick. */
import test from "node:test";
import assert from "node:assert/strict";
import { dueSlots, lastOccurrences } from "../lib/slots.ts";

const utc = (s: string) => new Date(s);

test("09:15 Baku is 05:15 UTC, and the tick at that minute starts it", () => {
  const due = dueSlots(utc("2026-09-24T05:15:00Z"));
  assert.deepEqual(due.map((s) => s.key), ["source_check:2026-09-24T09:15+04:00"]);
});

test("a tick a few minutes before the slot starts nothing", () => {
  assert.deepEqual(dueSlots(utc("2026-09-24T05:10:00Z")), []);
});

test("a late tick inside the catch-up window still starts the slot, with the same key", () => {
  const due = dueSlots(utc("2026-09-24T06:30:00Z"));
  assert.deepEqual(due.map((s) => s.key), ["source_check:2026-09-24T09:15+04:00"]);
});

test("an occurrence older than the window is left to the next one", () => {
  assert.deepEqual(dueSlots(utc("2026-09-24T07:00:00Z")), []);
});

test("the three daily checks are 09:15, 13:15 and 17:15 Baku", () => {
  const keys = ["05:20", "09:20", "13:20"].flatMap((t) => dueSlots(utc(`2026-09-24T${t}:00Z`)).map((s) => s.key));
  assert.deepEqual(keys, ["source_check:2026-09-24T09:15+04:00", "source_check:2026-09-24T13:15+04:00",
    "source_check:2026-09-24T17:15+04:00"]);
});

test("the weekly digest is Monday 08:30 Baku, and only Monday", () => {
  assert.deepEqual(dueSlots(utc("2026-09-28T04:30:00Z")).map((s) => s.key), ["weekly_digest:2026-09-28"]);
  assert.deepEqual(dueSlots(utc("2026-09-29T04:30:00Z")), []);
});

test("the Baku date is used near midnight UTC", () => {
  // 21:00 UTC Sunday is 01:00 Monday in Baku: nothing is due yet, and nothing from Sunday either
  assert.deepEqual(dueSlots(utc("2026-09-27T21:00:00Z")), []);
});

test("the last occurrence of each kind is known for monitoring", () => {
  const last = lastOccurrences(utc("2026-09-24T10:00:00Z"));
  assert.ok(last.some((s) => s.key === "source_check:2026-09-24T13:15+04:00"));
  assert.ok(last.some((s) => s.key === "weekly_digest:2026-09-21"));
});
