/**
 * When the system looks at the sources on its own. The times are Asia/Baku, as in
 * config/schedule.yaml; `azmonitor.scheduling.tasks.schedule_drift` fails CI if the two disagree.
 *
 * Baku has no daylight saving (UTC+4 all year), so a fixed offset is exact.
 */
export const SOURCE_CHECK_TIMES = ["09:15", "13:15", "17:15"] as const;
export const WEEKLY_DIGEST = { weekday: 1, time: "08:30" } as const;   // Monday
export const CATCH_UP_MINUTES = 90;
const BAKU_OFFSET_MINUTES = 4 * 60;

export interface Slot {
  kind: "source_check" | "weekly_digest";
  key: string;           // unique per occurrence: the database refuses a second job for it
  dueUtc: Date;
  label: string;
}

function bakuParts(now: Date) {
  const local = new Date(now.getTime() + BAKU_OFFSET_MINUTES * 60_000);
  return { date: local.toISOString().slice(0, 10), weekday: local.getUTCDay() };
}

function at(dateIso: string, hhmm: string): Date {
  return new Date(new Date(`${dateIso}T${hhmm}:00Z`).getTime() - BAKU_OFFSET_MINUTES * 60_000);
}

/**
 * The occurrences due now: scheduled at or before `now` and no older than the catch-up window.
 * A tick that runs late — or a tick that did not run at all until the next one — still starts the
 * occurrence it missed, once; an occurrence older than the window is left, because the next one
 * covers the same sources.
 */
export function dueSlots(now: Date): Slot[] {
  const out: Slot[] = [];
  const windowMs = CATCH_UP_MINUTES * 60_000;
  for (const offsetDays of [0, -1]) {
    const day = new Date(now.getTime() + offsetDays * 86_400_000);
    const { date, weekday } = bakuParts(day);
    for (const t of SOURCE_CHECK_TIMES) {
      const due = at(date, t);
      if (due <= now && now.getTime() - due.getTime() <= windowMs) {
        out.push({ kind: "source_check", key: `source_check:${date}T${t}+04:00`, dueUtc: due, label: `${date} ${t} Baku` });
      }
    }
    if (weekday === WEEKLY_DIGEST.weekday) {
      const due = at(date, WEEKLY_DIGEST.time);
      if (due <= now && now.getTime() - due.getTime() <= windowMs) {
        out.push({ kind: "weekly_digest", key: `weekly_digest:${date}`, dueUtc: due, label: `${date} ${WEEKLY_DIGEST.time} Baku` });
      }
    }
  }
  return out.sort((a, b) => a.dueUtc.getTime() - b.dueUtc.getTime());
}

/** The most recent occurrence of each kind at or before `now`, whether or not it is still due. */
export function lastOccurrences(now: Date): Slot[] {
  const found: Slot[] = [];
  for (let back = 0; back < 8 && found.filter((s) => s.kind === "weekly_digest").length === 0; back += 1) {
    const { date, weekday } = bakuParts(new Date(now.getTime() - back * 86_400_000));
    if (weekday === WEEKLY_DIGEST.weekday && at(date, WEEKLY_DIGEST.time) <= now) {
      found.push({ kind: "weekly_digest", key: `weekly_digest:${date}`, dueUtc: at(date, WEEKLY_DIGEST.time), label: date });
    }
  }
  for (let back = 0; back < 2; back += 1) {
    const { date } = bakuParts(new Date(now.getTime() - back * 86_400_000));
    const due = [...SOURCE_CHECK_TIMES].reverse().map((t) => ({ t, d: at(date, t) })).find((x) => x.d <= now);
    if (due) {
      found.push({ kind: "source_check", key: `source_check:${date}T${due.t}+04:00`, dueUtc: due.d, label: `${date} ${due.t}` });
      break;
    }
  }
  return found;
}
