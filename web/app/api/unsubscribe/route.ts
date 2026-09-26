/**
 * One-click unsubscribe (RFC 8058), authenticated by a signed token rather than a session: the
 * person clicking may not be signed in, and should not have to be. POST only — mail scanners
 * prefetch links with GET, and a GET that unsubscribed people would unsubscribe them at random.
 */
import { NextResponse } from "next/server";
import { ensureSchema, unsubscribe } from "@/lib/appstate";
import { verify } from "@/lib/signing";

export const runtime = "nodejs";

export async function POST(request: Request) {
  const token = new URL(request.url).searchParams.get("t");
  const recipientId = verify("unsubscribe", token);
  if (!recipientId) return NextResponse.json({ error: "This unsubscribe link is not valid." }, { status: 400 });
  await ensureSchema();
  const email = await unsubscribe(recipientId, "link");
  if (!email) return NextResponse.json({ error: "This unsubscribe link is not valid." }, { status: 400 });
  return NextResponse.json({ ok: true, note: "You will not receive further report announcements." });
}
