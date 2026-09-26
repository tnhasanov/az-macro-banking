import { NextResponse } from "next/server";
import { clearSuppression, recipients, upsertRecipient } from "@/lib/appstate";
import { handler, jsonBody, Refusal } from "@/lib/guard";
import { isReportType, SECTORS } from "@/lib/params";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

const EMAIL = /^[^\s@<>()",;]+@[^\s@<>()",;]+\.[^\s@<>()",;]{2,}$/;

export const GET = handler(async () => NextResponse.json({ recipients: await recipients() }), { admin: true });

export const POST = handler(async (request, caller) => {
  const body = await jsonBody<Record<string, unknown>>(request);
  if (body.action === "clear_suppression") {
    if (typeof body.recipient_id !== "string") throw new Refusal(400, "Name the recipient.", "bad_value");
    await clearSuppression(caller.subject, body.recipient_id);
    return NextResponse.json({ ok: true });
  }
  const email = typeof body.email === "string" ? body.email.trim() : "";
  if (!EMAIL.test(email) || email.length > 254) throw new Refusal(400, "That is not an email address.", "bad_email");
  const role = body.role === "owner" ? "owner" : "subscriber";
  const subs = Array.isArray(body.subscriptions) ? body.subscriptions : [];
  const subscriptions: { report_type: string; sector: string }[] = [];
  for (const s of subs.slice(0, 50)) {
    const rt = (s as Record<string, unknown>)?.report_type;
    const sector = String((s as Record<string, unknown>)?.sector ?? "*");
    if (!isReportType(rt)) throw new Refusal(400, `Unknown report type ${String(rt)}.`, "bad_value");
    if (sector !== "*" && !(SECTORS as readonly string[]).includes(sector)) throw new Refusal(400, `Unknown sector ${sector}.`, "bad_value");
    subscriptions.push({ report_type: rt, sector });
  }
  const id = await upsertRecipient(caller.subject, {
    email, displayName: typeof body.display_name === "string" ? body.display_name.slice(0, 120) : null,
    role, active: body.active !== false, subscriptions,
  });
  return NextResponse.json({ recipient_id: id });
}, { write: true, admin: true });
