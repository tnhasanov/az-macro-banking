/**
 * Who is allowed in.
 *
 * The dashboard carries the whole reporting history of a bank-facing monitor, so it is private and
 * stays private. Two mechanisms, deliberately separate:
 *
 *   - a **person** signs in with a passphrase and gets a short-lived signed session cookie;
 *   - a **scheduler** presents a bearer secret and may only trigger a run, never read a report.
 *
 * Neither can do the other's job. A stolen cron secret cannot open the archive; a session cookie
 * cannot fire a job. That separation is the point: the cron secret lives in Vercel's scheduler and
 * is replayed on every invocation, so it is the credential most likely to leak, and it is worth the
 * least.
 *
 * The passphrase is never stored. What is stored is a PBKDF2-SHA256 digest, verified with Web
 * Crypto so the same code runs on the Edge (middleware) and in Node (route handlers) without a
 * native dependency.
 */
import { SignJWT, jwtVerify } from "jose";

const COOKIE = "azmonitor_session";
const ISSUER = "azmonitor";
const AUDIENCE = "azmonitor-dashboard";
/** Eight hours: a working day, after which an unattended open laptop stops being a way in. */
const SESSION_SECONDS = 8 * 60 * 60;
/** OWASP's 2023 floor for PBKDF2-SHA256. Also the only brute-force brake this login has. */
const PBKDF2_ITERATIONS = 600_000;

export interface Session {
  sub: string;
  iat: number;
  exp: number;
}

function secret(): Uint8Array {
  const value = process.env.AZMONITOR_SESSION_SECRET;
  if (!value || value.length < 32) {
    throw new Error(
      "AZMONITOR_SESSION_SECRET is missing or shorter than 32 characters. " +
        "Generate one with: openssl rand -base64 48",
    );
  }
  return new TextEncoder().encode(value);
}

export async function issueSession(subject: string): Promise<{ token: string; maxAge: number }> {
  const token = await new SignJWT({})
    .setProtectedHeader({ alg: "HS256" })
    .setSubject(subject)
    .setIssuer(ISSUER)
    .setAudience(AUDIENCE)
    .setIssuedAt()
    .setExpirationTime(`${SESSION_SECONDS}s`)
    .sign(secret());
  return { token, maxAge: SESSION_SECONDS };
}

/** Returns the session or null. Never throws for a bad token — an invalid cookie is just "no". */
export async function readSession(token: string | undefined): Promise<Session | null> {
  if (!token) return null;
  try {
    const { payload } = await jwtVerify(token, secret(), {
      issuer: ISSUER,
      audience: AUDIENCE,
      algorithms: ["HS256"],
    });
    return payload as unknown as Session;
  } catch {
    return null;
  }
}

export const SESSION_COOKIE = COOKIE;

export function cookieOptions(maxAge: number) {
  return {
    name: COOKIE,
    httpOnly: true,
    sameSite: "lax" as const,
    secure: process.env.NODE_ENV === "production",
    path: "/",
    maxAge,
  };
}

// ---------------------------------------------------------------- passphrase

/** `pbkdf2$<iterations>$<salt-b64>$<hash-b64>` — self-describing, so the cost can be raised later. */
export async function hashPassphrase(passphrase: string, iterations = PBKDF2_ITERATIONS) {
  const salt = crypto.getRandomValues(new Uint8Array(16));
  const bits = await derive(passphrase, salt, iterations);
  return `pbkdf2$${iterations}$${b64(salt)}$${b64(new Uint8Array(bits))}`;
}

export async function verifyPassphrase(passphrase: string, stored: string): Promise<boolean> {
  const parts = stored.split("$");
  if (parts.length !== 4 || parts[0] !== "pbkdf2") return false;
  const iterations = Number.parseInt(parts[1], 10);
  if (!Number.isFinite(iterations) || iterations < 1) return false;
  const salt = unb64(parts[2]);
  const expected = unb64(parts[3]);
  const actual = new Uint8Array(await derive(passphrase, salt, iterations, expected.length * 8));
  return timingSafeEqual(actual, expected);
}

async function derive(passphrase: string, salt: Uint8Array, iterations: number, bits = 256) {
  const key = await crypto.subtle.importKey(
    "raw",
    new TextEncoder().encode(passphrase),
    "PBKDF2",
    false,
    ["deriveBits"],
  );
  return crypto.subtle.deriveBits(
    { name: "PBKDF2", salt: salt as BufferSource, iterations, hash: "SHA-256" },
    key,
    bits,
  );
}

/** Compares in time independent of where the first difference is. */
export function timingSafeEqual(a: Uint8Array, b: Uint8Array): boolean {
  if (a.length !== b.length) return false;
  let diff = 0;
  for (let i = 0; i < a.length; i += 1) diff |= a[i] ^ b[i];
  return diff === 0;
}

export function constantTimeStringEqual(a: string, b: string): boolean {
  const enc = new TextEncoder();
  return timingSafeEqual(enc.encode(a), enc.encode(b));
}

function b64(bytes: Uint8Array): string {
  let s = "";
  for (const byte of bytes) s += String.fromCharCode(byte);
  return btoa(s);
}

function unb64(text: string): Uint8Array {
  const bin = atob(text);
  const out = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i += 1) out[i] = bin.charCodeAt(i);
  return out;
}

// ---------------------------------------------------------------- scheduler

/**
 * Whether a request may trigger a scheduled task.
 *
 * Vercel Cron sends `Authorization: Bearer $CRON_SECRET`. Nothing else is accepted — in particular
 * not the `x-vercel-cron` header on its own, which is set by the platform and so proves only that
 * the request reached Vercel, not who sent it. If no secret is configured the answer is no: an
 * unprotected trigger endpoint on a public URL is an invitation to have the engine run by strangers.
 */
export function authorizeScheduler(request: Request): { ok: boolean; reason?: string } {
  const configured = process.env.CRON_SECRET;
  if (!configured || configured.length < 16) {
    return { ok: false, reason: "CRON_SECRET is not configured; refusing to trigger anything" };
  }
  const header = request.headers.get("authorization") ?? "";
  const presented = header.startsWith("Bearer ") ? header.slice(7) : "";
  if (!presented) return { ok: false, reason: "no bearer token" };
  return constantTimeStringEqual(presented, configured)
    ? { ok: true }
    : { ok: false, reason: "bearer token does not match" };
}
