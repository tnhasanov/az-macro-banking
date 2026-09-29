/**
 * The scheduler's entry point: Vercel Cron calls it every fifteen minutes on the production
 * deployment, authenticated by `Authorization: Bearer $CRON_SECRET`. See lib/scheduler.ts.
 *
 * It fails closed. If the database cannot be read, nothing is started and the response says so.
 */
import { NextResponse } from "next/server";
import { authorizeScheduler } from "@/lib/auth";
import { tick } from "@/lib/scheduler";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";
export const maxDuration = 60;

export async function GET(request: Request) {
  const auth = authorizeScheduler(request);
  if (!auth.ok) return NextResponse.json({ error: "unauthorised", reason: auth.reason }, { status: 401 });
  try {
    return NextResponse.json(await tick());
  } catch (error) {
    console.error("scheduler tick failed", error);
    return NextResponse.json({ error: "the tick could not complete; nothing further was started",
      detail: error instanceof Error ? error.name : "error" }, { status: 503 });
  }
}
