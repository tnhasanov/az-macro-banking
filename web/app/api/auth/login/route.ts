/**
 * Sign in.
 *
 * Runs on Node rather than the Edge because 600,000 PBKDF2 iterations is deliberately expensive and
 * the Edge runtime's CPU budget is not the place to spend it.
 */
import { NextResponse } from "next/server";
import { cookieOptions, issueSession, verifyPassphrase } from "@/lib/auth";

export const runtime = "nodejs";

/**
 * Best-effort throttle, per instance.
 *
 * A serverless deployment has no shared memory, so this slows an attacker down rather than stopping
 * them; the real brake is the PBKDF2 cost and a long passphrase. Stated plainly here because a
 * limiter that looks stronger than it is would be worse than none — see the readiness report.
 */
const attempts = new Map<string, { count: number; until: number }>();
const WINDOW_MS = 15 * 60 * 1000;
const MAX_ATTEMPTS = 10;

function throttled(key: string): boolean {
  const now = Date.now();
  const entry = attempts.get(key);
  if (!entry || entry.until < now) return false;
  return entry.count >= MAX_ATTEMPTS;
}

function record(key: string, failed: boolean) {
  const now = Date.now();
  if (!failed) return void attempts.delete(key);
  const entry = attempts.get(key);
  if (!entry || entry.until < now) attempts.set(key, { count: 1, until: now + WINDOW_MS });
  else entry.count += 1;
}

export async function POST(request: Request) {
  const stored = process.env.AZMONITOR_DASHBOARD_PASSPHRASE_HASH;
  if (!stored) {
    return NextResponse.json(
      { error: "No passphrase is configured for this deployment." },
      { status: 503 },
    );
  }

  const ip = request.headers.get("x-forwarded-for")?.split(",")[0]?.trim() || "unknown";
  if (throttled(ip)) {
    return NextResponse.json(
      { error: "Too many attempts. Wait fifteen minutes and try again." },
      { status: 429 },
    );
  }

  let passphrase = "";
  try {
    const body = await request.json();
    passphrase = typeof body?.passphrase === "string" ? body.passphrase : "";
  } catch {
    return NextResponse.json({ error: "Malformed request." }, { status: 400 });
  }

  const ok = passphrase.length > 0 && (await verifyPassphrase(passphrase, stored));
  record(ip, !ok);
  if (!ok) {
    // One message for every failure: naming which part was wrong would be a free hint.
    return NextResponse.json({ error: "That passphrase was not accepted." }, { status: 401 });
  }

  const { token, maxAge } = await issueSession(process.env.AZMONITOR_OWNER_EMAIL || "owner");
  const response = NextResponse.json({ ok: true });
  response.cookies.set({ ...cookieOptions(maxAge), value: token });
  return response;
}
