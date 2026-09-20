/**
 * The gate.
 *
 * Every path is private unless it is named here, which is the safe direction for the list to fail
 * in: a page added later is protected because nobody remembered to protect it, not exposed because
 * nobody remembered to.
 *
 * The scheduled-task endpoints are exempt from the *session* check only. They authenticate
 * themselves with a bearer secret (see `authorizeScheduler`), because a cron invocation has no
 * cookie to present. Exempt here does not mean open there.
 */
import { NextResponse, type NextRequest } from "next/server";
import { readSession, SESSION_COOKIE } from "./lib/auth";

const PUBLIC_PATHS = ["/login", "/api/auth/login"];
/** Authenticated by bearer secret inside the handler rather than by a session cookie. */
const SELF_AUTHENTICATING = ["/api/cron/"];

export async function middleware(request: NextRequest) {
  const { pathname, search } = request.nextUrl;

  if (SELF_AUTHENTICATING.some((p) => pathname.startsWith(p))) return NextResponse.next();
  if (PUBLIC_PATHS.includes(pathname)) return NextResponse.next();

  // A misconfigured deployment must refuse to serve, not serve without a check.
  if (!process.env.AZMONITOR_SESSION_SECRET) {
    return new NextResponse(
      "This deployment has no AZMONITOR_SESSION_SECRET, so it cannot verify a sign-in and will " +
        "not serve anything.",
      { status: 503, headers: { "content-type": "text/plain; charset=utf-8" } },
    );
  }

  const session = await readSession(request.cookies.get(SESSION_COOKIE)?.value);
  if (session) return NextResponse.next();

  // An API caller gets a status code it can act on; a person gets the sign-in page and their
  // destination back afterwards.
  if (pathname.startsWith("/api/")) {
    return NextResponse.json({ error: "not signed in" }, { status: 401 });
  }
  const url = request.nextUrl.clone();
  url.pathname = "/login";
  url.search = "";
  if (pathname !== "/") url.searchParams.set("next", pathname + search);
  return NextResponse.redirect(url);
}

export const config = {
  // Everything except Next's own static output and the icons the browser asks for unauthenticated.
  matcher: ["/((?!_next/static|_next/image|favicon.ico|icon.svg|robots.txt).*)"],
};
