/**
 * Signed, purpose-bound tokens for links that must work without a session — today only the
 * unsubscribe link in a subscription email. The key is derived from the session secret with the
 * purpose mixed in, so a token minted for one purpose can never be replayed as another.
 */
import { createHmac, timingSafeEqual } from "node:crypto";

function key(purpose: string): Buffer {
  const secret = process.env.AZMONITOR_SESSION_SECRET;
  if (!secret || secret.length < 32) throw new Error("AZMONITOR_SESSION_SECRET is not configured");
  return createHmac("sha256", secret).update(`azmonitor:${purpose}`).digest();
}

export function sign(purpose: string, value: string): string {
  const mac = createHmac("sha256", key(purpose)).update(value).digest("base64url");
  return `${Buffer.from(value).toString("base64url")}.${mac}`;
}

export function verify(purpose: string, token: string | null | undefined): string | null {
  if (!token || token.length > 400) return null;
  const [encoded, mac] = token.split(".");
  if (!encoded || !mac) return null;
  const value = Buffer.from(encoded, "base64url").toString();
  const expected = createHmac("sha256", key(purpose)).update(value).digest();
  const given = Buffer.from(mac, "base64url");
  return given.length === expected.length && timingSafeEqual(given, expected) ? value : null;
}
