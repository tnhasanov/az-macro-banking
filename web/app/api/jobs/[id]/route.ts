import { NextResponse } from "next/server";
import { handler, Refusal } from "@/lib/guard";
import { JOB_ID_PATTERN } from "@/lib/ids";
import { jobView } from "@/lib/jobview";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

export const GET = handler<{ params: Promise<{ id: string }> }>(async (_request, caller, { params }) => {
  const { id } = await params;
  if (!JOB_ID_PATTERN.test(id)) throw new Refusal(404, "No such job.", "not_found");
  const view = await jobView(id, caller.email);
  if (!view) throw new Refusal(404, "No such job.", "not_found");
  return NextResponse.json(view, { headers: { "cache-control": "no-store" } });
});
