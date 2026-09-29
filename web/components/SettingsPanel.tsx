"use client";

import { useRouter } from "next/navigation";
import { useState } from "react";
import { postJSON } from "@/lib/client";
import { REPORT_TYPES, type ReportType } from "@/lib/params";

interface Settings {
  auto_email_enabled: boolean; auto_checks_enabled: boolean; paused: boolean; paused_reason: string | null;
  notify_revisions: boolean; attach_pdf: boolean; revision_settle_minutes: number; updated_at: string | null;
  updated_by: string | null;
}
interface Recipient {
  recipient_id: string; email: string; display_name: string | null; role: "owner" | "subscriber"; active: boolean;
  unsubscribed_at: string | null; suppressed_at: string | null; suppressed_reason: string | null;
  subscriptions: { report_type: string; sector: string }[];
}
interface LedgerRow {
  delivery_id: string; to: string; purpose: string; status: string; status_reason: string | null; attempts: number;
  created_at: string; edition: string | null; last_error: string | null; last_event: string | null;
}

const TYPES = Object.entries(REPORT_TYPES) as [ReportType, { label: string }][];

export function SettingsPanel(props: { settings: Settings; recipients: Recipient[]; ledger: LedgerRow[];
  sectors: string[]; canEmail: boolean }) {
  const router = useRouter();
  const [s, setS] = useState(props.settings);
  const [msg, setMsg] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [editing, setEditing] = useState<{ email: string; role: "owner" | "subscriber"; types: Set<string>;
    sectors: Set<string>; active: boolean } | null>(null);

  async function run(fn: () => Promise<string | void>) {
    setBusy(true);
    setMsg(null);
    try {
      const m = await fn();
      setMsg(m || "Saved.");
      router.refresh();
    } catch (e) {
      setMsg(e instanceof Error ? e.message : String(e));
    }
    setBusy(false);
  }

  function save(patch: Partial<Settings>) {
    void run(async () => {
      const next = await postJSON<Settings>("/api/settings", patch);
      setS(next);
    });
  }

  function edit(r?: Recipient) {
    setEditing({
      email: r?.email ?? "", role: r?.role ?? "subscriber", active: r ? !r.unsubscribed_at && r.active : true,
      types: new Set((r?.subscriptions ?? []).map((x) => x.report_type)),
      sectors: new Set((r?.subscriptions ?? []).filter((x) => x.report_type === "sector" && x.sector !== "*").map((x) => x.sector)),
    });
  }

  function saveRecipient() {
    if (!editing) return;
    const subscriptions: { report_type: string; sector: string }[] = [];
    for (const t of editing.types) {
      if (t === "sector" && editing.sectors.size) for (const sec of editing.sectors) subscriptions.push({ report_type: t, sector: sec });
      else subscriptions.push({ report_type: t, sector: "*" });
    }
    void run(async () => {
      await postJSON("/api/settings/recipients", { email: editing.email, role: editing.role, active: editing.active, subscriptions });
      setEditing(null);
      return "Recipient saved.";
    });
  }

  const toggle = (key: keyof Settings, label: string, sub: string) => (
    <label className="check">
      <input type="checkbox" checked={Boolean(s[key])} disabled={busy} onChange={(e) => save({ [key]: e.target.checked })} />
      <span>{label}<span className="sub">{sub}</span></span>
    </label>
  );

  return (
    <div className="stack">
      {msg && <div className="notice" role="status">{msg}</div>}

      <section className="card">
        <header className="card-head">
          <div>
            <h3 className="card-title">Automation</h3>
            <div className="card-note">{s.updated_at ? `Last changed ${s.updated_at.slice(0, 16).replace("T", " ")} UTC by ${s.updated_by}` : "Never changed: everything automatic is off."}</div>
          </div>
        </header>
        <div className="form-grid">
          {toggle("auto_checks_enabled", "Check official sources automatically",
            "At 09:15, 13:15 and 17:15 Baku, and the weekly digest on Monday at 08:30. Production only.")}
          {toggle("auto_email_enabled", "Email subscribers about new editions",
            "Only editions caused by new data, a revision or a new publication. Never backfills, translations or reused editions.")}
          {toggle("notify_revisions", "Include revised editions", "A revision of an edition already announced sends a revision notice.")}
          {toggle("attach_pdf", "Attach the PDF when it is small enough", "Otherwise the email links to it; links always need signing in.")}
        </div>
        <div className="row" style={{ gap: 10, marginTop: 14 }}>
          <label className="field" style={{ maxWidth: 220 }}>
            <span style={{ fontSize: 12.5, fontWeight: 600 }}>Revision settle window (minutes)</span>
            <input type="number" min={0} max={1440} defaultValue={s.revision_settle_minutes}
              onBlur={(e) => Number(e.target.value) !== s.revision_settle_minutes && save({ revision_settle_minutes: Number(e.target.value) })} />
          </label>
          {s.paused ? (
            <button type="button" disabled={busy} onClick={() => save({ paused: false, paused_reason: null })}>Resume notifications</button>
          ) : (
            <button type="button" disabled={busy} onClick={() => save({ paused: true, paused_reason: "paused from Settings" })}>Pause notifications</button>
          )}
          {s.paused && <span className="badge badge-warning">Paused: announcements are held and go out on resume</span>}
          {props.canEmail && (
            <button type="button" disabled={busy} onClick={() => void run(async () => {
              const r = await postJSON<{ outcome: string; provider: string }>("/api/settings/test-email");
              return `Test email: ${r.outcome} (provider ${r.provider}).`;
            })}>Send a test email to me</button>
          )}
        </div>
      </section>

      <section className="card">
        <header className="card-head">
          <div>
            <h3 className="card-title">Recipients</h3>
            <div className="card-note">Subscription email goes to active recipients for the report types they follow. A manual “email me” goes only to whoever asked.</div>
          </div>
          <button type="button" onClick={() => edit()} disabled={busy}>Add recipient</button>
        </header>
        <table className="data">
          <thead><tr><th>Address</th><th>Role</th><th>Follows</th><th>State</th><th /></tr></thead>
          <tbody>
            {props.recipients.map((r) => (
              <tr key={r.recipient_id}>
                <td>{r.email}</td>
                <td>{r.role}</td>
                <td>{r.subscriptions.map((x) => `${REPORT_TYPES[x.report_type as ReportType]?.label ?? x.report_type}${x.sector !== "*" ? ` (${x.sector})` : ""}`).join(", ") || "nothing"}</td>
                <td>{r.unsubscribed_at ? "unsubscribed" : !r.active ? "inactive" : r.suppressed_at ? `suppressed: ${r.suppressed_reason}` : "active"}</td>
                <td className="nowrap">
                  <button type="button" onClick={() => edit(r)} disabled={busy}>Edit</button>{" "}
                  {r.suppressed_at && <button type="button" disabled={busy} onClick={() => void run(async () => {
                    await postJSON("/api/settings/recipients", { action: "clear_suppression", recipient_id: r.recipient_id });
                    return "Suppression cleared.";
                  })}>Clear suppression</button>}
                </td>
              </tr>
            ))}
            {props.recipients.length === 0 && <tr><td colSpan={5} className="muted">No recipients. Add the owner first.</td></tr>}
          </tbody>
        </table>
        {editing && (
          <div className="stack" style={{ marginTop: 14, gap: 10 }}>
            <div className="form-grid">
              <label className="field"><span style={{ fontSize: 12.5, fontWeight: 600 }}>Email address</span>
                <input type="email" value={editing.email} onChange={(e) => setEditing({ ...editing, email: e.target.value })} /></label>
              <label className="field"><span style={{ fontSize: 12.5, fontWeight: 600 }}>Role</span>
                <select value={editing.role} onChange={(e) => setEditing({ ...editing, role: e.target.value as "owner" | "subscriber" })}>
                  <option value="owner">owner (also receives alerts)</option><option value="subscriber">subscriber</option>
                </select></label>
            </div>
            <div className="choice-grid">
              {TYPES.map(([id, spec]) => (
                <label key={id} className="choice">
                  <input type="checkbox" checked={editing.types.has(id)} onChange={(e) => {
                    const types = new Set(editing.types);
                    if (e.target.checked) types.add(id); else types.delete(id);
                    setEditing({ ...editing, types });
                  }} />
                  <span>{spec.label}</span>
                </label>
              ))}
            </div>
            {editing.types.has("sector") && (
              <div className="row" style={{ gap: 12 }}>
                <span className="muted" style={{ fontSize: 12.5 }}>Sectors (none ticked = all):</span>
                {props.sectors.map((sec) => (
                  <label key={sec} className="check"><input type="checkbox" checked={editing.sectors.has(sec)} onChange={(e) => {
                    const sectors = new Set(editing.sectors);
                    if (e.target.checked) sectors.add(sec); else sectors.delete(sec);
                    setEditing({ ...editing, sectors });
                  }} />{sec}</label>
                ))}
              </div>
            )}
            <label className="check"><input type="checkbox" checked={editing.active} onChange={(e) => setEditing({ ...editing, active: e.target.checked })} />Active</label>
            <div className="row" style={{ gap: 8 }}>
              <button type="button" className="primary" onClick={saveRecipient} disabled={busy}>Save recipient</button>
              <button type="button" onClick={() => setEditing(null)} disabled={busy}>Cancel</button>
            </div>
          </div>
        )}
      </section>

      <section className="card">
        <header className="card-head">
          <div>
            <h3 className="card-title">Delivery ledger</h3>
            <div className="card-note">Accepted means the provider has the message; delivered means it confirmed delivery. Uncertain means it may or may not have been sent.</div>
          </div>
        </header>
        <div className="table-scroll">
          <table className="data">
            <thead><tr><th>Created</th><th>To</th><th>Purpose</th><th>Edition</th><th>Status</th><th>Detail</th><th /></tr></thead>
            <tbody>
              {props.ledger.map((r) => (
                <tr key={r.delivery_id}>
                  <td className="nowrap">{r.created_at.slice(0, 16).replace("T", " ")}</td>
                  <td>{r.to}</td>
                  <td>{r.purpose.replace(/_/g, " ")}</td>
                  <td>{r.edition ?? ""}</td>
                  <td><span className="badge badge-neutral">{r.status}</span><span className="sub">{r.attempts} attempt(s)</span></td>
                  <td className="muted" style={{ fontSize: 12.5, maxWidth: 320 }}>{r.status_reason ?? ""} {r.last_error ?? ""} {r.last_event ? `(${r.last_event})` : ""}</td>
                  <td className="nowrap">
                    {r.status === "failed" && <button type="button" disabled={busy} onClick={() => void run(async () => {
                      await postJSON(`/api/deliveries/${r.delivery_id}`, { action: "retry" }); return "Retry queued.";
                    })}>Retry</button>}
                    {r.status === "uncertain" && <>
                      <button type="button" disabled={busy} onClick={() => void run(async () => {
                        await postJSON(`/api/deliveries/${r.delivery_id}`, { action: "mark_sent" }); return "Marked as sent.";
                      })}>It arrived</button>{" "}
                      <button type="button" disabled={busy} onClick={() => void run(async () => {
                        await postJSON(`/api/deliveries/${r.delivery_id}`, { action: "resend" }); return "Sent again.";
                      })}>Send again</button>
                    </>}
                  </td>
                </tr>
              ))}
              {props.ledger.length === 0 && <tr><td colSpan={7} className="muted">Nothing has been queued in this environment.</td></tr>}
            </tbody>
          </table>
        </div>
      </section>
    </div>
  );
}
