/**
 * Why the application database could not be used, in words that name the fix.
 *
 * A page that only says "could not be reached" leaves the owner nothing to act on, and the owner
 * cannot read the runtime logs from where they are. The cause is almost always one of a handful —
 * the variable is not set for this environment, the value is not a connection string, the host is
 * wrong, the password was rejected — and each has a different fix.
 *
 * Nothing secret is ever returned: never the connection string, the host, the user or the
 * driver's own message (which can carry the user name). Only which variable was read, and a
 * description chosen from the error's code.
 */

export const DATABASE_VARIABLES = ["AZMONITOR_DATABASE_URL", "POSTGRES_URL", "DATABASE_URL"] as const;

/** The variable the connection string was read from, or null. The name, never the value. */
export function databaseSource(env: Record<string, string | undefined> = process.env): string | null {
  for (const name of DATABASE_VARIABLES) {
    if (env[name]?.trim()) return name;
  }
  return null;
}

const BY_CODE: Record<string, string> = {
  ENOTFOUND: "the database host it names was not found — check the host part of the value",
  EAI_AGAIN: "the database host it names could not be looked up — check the host part of the value",
  ECONNREFUSED: "the database refused the connection",
  ECONNRESET: "the database closed the connection while it was being opened",
  ETIMEDOUT: "the connection timed out",
  CONNECT_TIMEOUT: "the connection timed out",
  "28P01": "the database rejected the password — copy the connection string again from Neon",
  "28000": "the database rejected the connection's user or security settings — the value should end with ?sslmode=require",
  "3D000": "the database named in it does not exist",
  "08P01": "the database refused the connection's settings — the value should end with ?sslmode=require",
  ERR_INVALID_URL:
    "it is not a valid connection string — it should start with postgresql:// and have no quotes, spaces or leading 'psql'",
  ERR_INVALID_URL_SCHEME: "it is not a postgresql:// connection string",
};

export function databaseProblem(
  error: unknown,
  env: Record<string, string | undefined> = process.env,
): string {
  const source = databaseSource(env);
  if (!source) {
    return "AZMONITOR_DATABASE_URL is not set for this deployment's environment. In Vercel: " +
      "Settings → Environment Variables, add it for Production, then redeploy.";
  }
  const e = (error ?? {}) as { code?: unknown; name?: unknown };
  const code = typeof e.code === "string" ? e.code : "";
  const known = BY_CODE[code];
  if (known) return `${source} is set, but ${known}.`;
  const name = typeof e.name === "string" && e.name ? e.name : "an error";
  return `${source} is set, but connecting failed with ${name}${code ? ` (${code})` : ""}.`;
}
