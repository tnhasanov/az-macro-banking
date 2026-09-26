/**
 * The checks every state-changing API route makes, in one place so none is forgotten.
 *
 *  - **Who**: the session cookie is verified in the handler, not only in middleware. Middleware is a
 *    path regex; a route that starts a runner or sends email should not depend on nobody editing it.
 *  - **Whether they may**: administrative actions (forcing a regeneration, changing who is emailed)
 *    need an administrator. With one passphrase there is one person, but the check is here so that a
 *    second, read-only login cannot inherit those rights by accident.
 *  - **From where**: a state-changing request must come from this application's own pages. The
 *    session cookie is SameSite=Lax, which already stops a cross-site form post from carrying it;
 *    the Origin check and the JSON content type are the second layer, and a cross-origin `fetch`
 *    with a JSON body cannot be sent without a CORS preflight this app never answers.
 *  - **How often**: a per-user, per-action limit held in Postgres, so it holds across instances.
 */
import { NextResponse } from "next/server";
import { readSession, SESSION_COOKIE, type Session } from "./auth";
import { ensureSchema, withinLimit } from "./appstate";

export interface Caller {
  subject: string;
  email: string | null;
  admin: boolean;
}

export class Refusal extends Error {
  status: number;
  code: string;

  constructor(status: number, message: string, code = "refused") {
    super(message);
    this.status = status;
    this.code = code;
  }
}

function cookie(request: Request, name: string): string | undefined {
  return (request.headers.get("cookie") ?? "").split(";").map((c) => c.trim())
    .find((c) => c.startsWith(`${name}=`))?.slice(name.length + 1);
}

const EMAIL = /^[^\s@<>()",;]+@[^\s@<>()",;]+\.[^\s@<>()",;]+$/;

export function callerFrom(session: Session): Caller {
  const admins = (process.env.AZMONITOR_ADMIN_SUBJECTS ?? process.env.AZMONITOR_OWNER_EMAIL ?? "owner")
    .split(",").map((s) => s.trim().toLowerCase()).filter(Boolean);
  const subject = String(session.sub ?? "");
  return {
    subject,
    // The session subject is the owner's address when one is configured; that is the only address
    // a "email me" request can ever go to. Nothing typed into the browser chooses a recipient.
    email: EMAIL.test(subject) ? subject : null,
    admin: admins.includes(subject.toLowerCase()),
  };
}

export async function requireCaller(request: Request): Promise<Caller> {
  const session = await readSession(cookie(request, SESSION_COOKIE));
  if (!session) throw new Refusal(401, "Not signed in.", "unauthenticated");
  return callerFrom(session);
}

/** Same-origin check for anything that changes state. */
export function requireSameOrigin(request: Request): void {
  const origin = request.headers.get("origin");
  const site = request.headers.get("sec-fetch-site");
  const host = request.headers.get("x-forwarded-host") ?? request.headers.get("host");
  if (site && site !== "same-origin") {
    throw new Refusal(403, "Cross-site requests are refused.", "cross_site");
  }
  if (origin) {
    let ok = false;
    try {
      ok = new URL(origin).host === host;
    } catch {
      ok = false;
    }
    if (!ok) throw new Refusal(403, "Cross-site requests are refused.", "cross_site");
  } else if (!site) {
    // Neither header: not something a current browser sends for a same-origin fetch.
    throw new Refusal(403, "The request did not say where it came from.", "cross_site");
  }
}

export async function jsonBody<T = Record<string, unknown>>(request: Request, maxBytes = 16_384): Promise<T> {
  const type = request.headers.get("content-type") ?? "";
  if (!type.toLowerCase().startsWith("application/json")) {
    throw new Refusal(415, "Send JSON.", "unsupported_media_type");
  }
  const text = await request.text();
  if (text.length > maxBytes) throw new Refusal(413, "The request is too large.", "too_large");
  try {
    const value = JSON.parse(text || "{}");
    if (typeof value !== "object" || value === null || Array.isArray(value)) throw new Error("not an object");
    return value as T;
  } catch {
    throw new Refusal(400, "The request body is not valid JSON.", "malformed");
  }
}

export async function throttle(caller: Caller, action: string, limit: number, windowSeconds: number) {
  if (!(await withinLimit(`${action}:${caller.subject}`, limit, windowSeconds))) {
    throw new Refusal(429, `Too many requests of this kind. Try again in ${Math.ceil(windowSeconds / 60)} minutes.`,
      "throttled");
  }
}

/**
 * Wrap a handler: session, same-origin for writes, schema, and errors turned into JSON a person
 * can read. An unexpected error returns a generic message; its detail stays in the server log.
 */
export function handler<C>(
  fn: (request: Request, caller: Caller, context: C) => Promise<Response>,
  opts: { write?: boolean; admin?: boolean } = {},
) {
  return async (request: Request, context: C): Promise<Response> => {
    try {
      const caller = await requireCaller(request);
      if (opts.write) requireSameOrigin(request);
      if (opts.admin && !caller.admin) throw new Refusal(403, "This needs an administrator.", "forbidden");
      await ensureSchema();
      return await fn(request, caller, context);
    } catch (error) {
      if (error instanceof Refusal) {
        return NextResponse.json({ error: error.message, code: error.code }, { status: error.status });
      }
      console.error("request failed", error);
      return NextResponse.json({ error: "The request could not be completed. The server log has the details." },
        { status: 500 });
    }
  };
}
