import { NextResponse } from "next/server";
import { environment, notificationSettings, saveSettings } from "@/lib/appstate";
import { handler, jsonBody, Refusal } from "@/lib/guard";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

export const GET = handler(async () => NextResponse.json(await notificationSettings(environment())));

const BOOLEAN_KEYS = ["auto_email_enabled", "auto_checks_enabled", "paused", "notify_revisions", "attach_pdf"] as const;

export const POST = handler(async (request, caller) => {
  const body = await jsonBody<Record<string, unknown>>(request);
  const next: Record<string, unknown> = {};
  for (const k of BOOLEAN_KEYS) {
    if (k in body) {
      if (typeof body[k] !== "boolean") throw new Refusal(400, `${k} must be true or false.`, "bad_value");
      next[k] = body[k];
    }
  }
  if ("revision_settle_minutes" in body) {
    const n = Number(body.revision_settle_minutes);
    if (!Number.isInteger(n) || n < 0 || n > 1440) throw new Refusal(400, "The settle window is 0 to 1440 minutes.", "bad_value");
    next.revision_settle_minutes = n;
  }
  if ("paused_reason" in body) next.paused_reason = typeof body.paused_reason === "string" ? body.paused_reason.slice(0, 300) : null;
  const saved = await saveSettings(environment(), caller.subject, next);
  return NextResponse.json(saved);
}, { write: true, admin: true });
