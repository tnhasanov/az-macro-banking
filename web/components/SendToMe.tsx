"use client";

import { useState } from "react";
import { postJSON } from "@/lib/client";

/** "Send this existing report to me": to the signed-in person's own address, nobody else's. */
export function SendToMe({ editionId, canEmail }: { editionId: string; canEmail: boolean }) {
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState<string | null>(null);
  if (!canEmail) return null;
  async function send() {
    setBusy(true);
    try {
      const r = await postJSON<{ note: string }>("/api/editions/send", { edition_id: editionId });
      setNote(r.note);
    } catch (e) {
      setNote(e instanceof Error ? e.message : String(e));
    }
    setBusy(false);
  }
  return (
    <div className="row" style={{ gap: 10, marginTop: 12 }}>
      <button type="button" onClick={() => void send()} disabled={busy}>Send this report to me</button>
      {note && <span className="muted" style={{ fontSize: 12.5 }}>{note}</span>}
    </div>
  );
}
