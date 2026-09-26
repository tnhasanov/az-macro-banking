import { NextResponse } from "next/server";
import { environment, publication } from "@/lib/appstate";
import { drainOutbox, queueCopy } from "@/lib/email/outbox";
import { handler, jsonBody, Refusal, throttle } from "@/lib/guard";

export const runtime = "nodejs";

/** "Send this existing report to me" — to the signed-in person's own address and nobody else's. */
export const POST = handler(async (request, caller) => {
  await throttle(caller, "email", 20, 3600);
  const body = await jsonBody<{ edition_id?: unknown }>(request);
  const editionId = typeof body.edition_id === "string" ? body.edition_id : "";
  if (!/^[a-z_]+:[A-Za-z0-9_.\-]+:v\d+$/.test(editionId)) throw new Refusal(400, "Name an edition.", "bad_edition");
  if (!caller.email) throw new Refusal(400, "This sign-in has no email address configured.", "no_email");
  const p = await publication(editionId);
  if (!p) throw new Refusal(404, "That edition is not published.", "not_found");
  const env = environment();
  const deliveryId = await queueCopy(env, editionId, caller.email);
  const sent = await drainOutbox(env, { deliveryId }).catch(() => null);
  const outcome = sent?.outcomes?.[0]?.outcome ?? "queued";
  return NextResponse.json({ delivery_id: deliveryId, outcome,
    note: outcome === "accepted" ? "Sent to the email provider. It is marked delivered when the provider confirms it."
      : outcome === "suppressed" ? "Recorded, but this deployment does not send email."
      : "Queued; it will be retried automatically." });
}, { write: true });
