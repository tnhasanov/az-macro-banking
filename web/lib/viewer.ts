/** The signed-in person, for server components. Middleware has already refused anyone else. */
import { cookies } from "next/headers";
import { readSession, SESSION_COOKIE } from "./auth";
import { callerFrom, type Caller } from "./guard";

export async function viewer(): Promise<Caller | null> {
  const jar = await cookies();
  const session = await readSession(jar.get(SESSION_COOKIE)?.value);
  return session ? callerFrom(session) : null;
}
