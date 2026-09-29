import { NextResponse } from "next/server";
import { environment } from "@/lib/appstate";
import { drainOutbox, queueTest } from "@/lib/email/outbox";
import { handler, Refusal, throttle } from "@/lib/guard";

export const runtime = "nodejs";

/** A test message to the signed-in administrator, through exactly the path real email takes. */
export const POST = handler(async (_request, caller) => {
  await throttle(caller, "test_email", 5, 3600);
  if (!caller.email) throw new Refusal(400, "This sign-in has no email address configured.", "no_email");
  const env = environment();
  const deliveryId = await queueTest(env, caller.email);
  const res = await drainOutbox(env, { deliveryId });
  const outcome = res.outcomes[0]?.outcome ?? "queued";
  return NextResponse.json({ delivery_id: deliveryId, provider: res.provider, outcome });
}, { write: true, admin: true });
