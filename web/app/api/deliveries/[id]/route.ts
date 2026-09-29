import { NextResponse } from "next/server";
import { environment } from "@/lib/appstate";
import { drainOutbox, resolveUncertain, retryDelivery } from "@/lib/email/outbox";
import { handler, jsonBody, Refusal } from "@/lib/guard";

export const runtime = "nodejs";

/** Acting on one delivery: retry a failure, or settle one that may or may not have been sent. */
export const POST = handler<{ params: Promise<{ id: string }> }>(async (request, caller, { params }) => {
  const { id } = await params;
  if (!/^dlv_[0-9a-hjkmnp-tv-z]{26}$/.test(id)) throw new Refusal(404, "No such delivery.", "not_found");
  const body = await jsonBody<{ action?: unknown }>(request);
  if (body.action === "retry") {
    if (!(await retryDelivery(id, caller.subject))) throw new Refusal(409, "Only a failed delivery can be retried.", "not_failed");
    await drainOutbox(environment(), { deliveryId: id }).catch(() => undefined);
  } else if (body.action === "mark_sent" || body.action === "resend") {
    await resolveUncertain(id, caller.subject, body.action === "mark_sent" ? "sent" : "resend");
    if (body.action === "resend") await drainOutbox(environment(), { deliveryId: id }).catch(() => undefined);
  } else {
    throw new Refusal(400, "Choose retry, mark_sent or resend.", "bad_action");
  }
  return NextResponse.json({ ok: true });
}, { write: true, admin: true });
