/**
 * Delivery events from Resend.
 *
 * Authenticated by signature, not by session: Resend signs each delivery with the webhook's secret
 * (Svix headers), and the official SDK verifies the signature and the timestamp before anything is
 * read. An unsigned or replayed request is refused. A redelivered event is recognised by its id and
 * changes nothing; an event that arrives before the send it describes is recorded and applied when
 * that send is.
 */
import { NextResponse } from "next/server";
import { ensureSchema } from "@/lib/appstate";
import { recordEvent, type ProviderEvent } from "@/lib/email/outbox";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

export async function POST(request: Request) {
  const secret = process.env.RESEND_WEBHOOK_SECRET;
  if (!secret) return NextResponse.json({ error: "webhooks are not configured" }, { status: 503 });
  const id = request.headers.get("svix-id");
  const timestamp = request.headers.get("svix-timestamp");
  const signature = request.headers.get("svix-signature");
  if (!id || !timestamp || !signature) return NextResponse.json({ error: "unsigned" }, { status: 401 });
  const payload = await request.text();
  if (payload.length > 256_000) return NextResponse.json({ error: "too large" }, { status: 413 });

  let event: ProviderEvent;
  try {
    const { Resend } = await import("resend");
    event = new Resend(process.env.RESEND_API_KEY ?? "re_unused").webhooks.verify({
      payload, headers: { id, timestamp, signature }, webhookSecret: secret,
    }) as unknown as ProviderEvent;
  } catch {
    return NextResponse.json({ error: "signature did not verify" }, { status: 401 });
  }
  await ensureSchema();
  const outcome = await recordEvent("resend", { ...event, id });
  return NextResponse.json({ ok: true, outcome });
}
