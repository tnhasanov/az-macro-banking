/**
 * Who is emailed, about what, and whether the system checks the sources on its own.
 *
 * Both automatic behaviours start off. Turning them on is a decision recorded in the audit log,
 * with the person who made it.
 */
import { ensureSchema, environment, meta, notificationSettings, recipients } from "@/lib/appstate";
import { dispatcher } from "@/lib/dispatch";
import { ledger } from "@/lib/email/outbox";
import { emailProvider } from "@/lib/email/provider";
import { viewer } from "@/lib/viewer";
import { Card, Empty } from "@/components/ui";
import { SettingsPanel } from "@/components/SettingsPanel";
import { SECTORS } from "@/lib/params";

export const dynamic = "force-dynamic";

export default async function SettingsPage() {
  await ensureSchema();
  const env = environment();
  const who = await viewer();
  if (!who?.admin) {
    return (<><header className="topbar"><h1>Settings</h1></header>
      <div className="content"><Empty title="Settings need an administrator." /></div></>);
  }
  const [settings, people, rows, lastTick] = await Promise.all([
    notificationSettings(env), recipients(), ledger(env, 60),
    meta<{ at: string; problems: string[]; scheduling: string }>("scheduler_tick"),
  ]);
  const provider = emailProvider();
  const d = dispatcher(env);
  return (
    <>
      <header className="topbar">
        <div>
          <h1>Settings</h1>
          <p>Automatic checks, automatic email, recipients and the delivery ledger for the <strong>{env}</strong> environment.</p>
        </div>
      </header>
      <div className="content">
        <div className="grid grid-2" style={{ alignItems: "start" }}>
          <Card title="This deployment">
            <table className="data"><tbody>
              <tr><td>Environment</td><td>{env}{env !== "production" ? " — never emails subscribers" : ""}</td></tr>
              <tr><td>Email provider</td><td>{"send" in provider ? provider.name : `none: ${provider.reason}`}</td></tr>
              <tr><td>Runner dispatch</td><td>{"dispatch" in d ? d.name : `off: ${d.reason}`}</td></tr>
              <tr><td>Scheduler</td><td>{lastTick ? `last tick ${lastTick.at.slice(0, 16).replace("T", " ")} UTC — ${lastTick.scheduling}` : "has not run in this database yet"}</td></tr>
              <tr><td>Your address</td><td>{who.email ?? "not configured (AZMONITOR_OWNER_EMAIL)"}</td></tr>
            </tbody></table>
            {lastTick?.problems?.length ? (
              <div className="notice" style={{ marginTop: 12 }}>
                <strong>Needs attention</strong>
                <ul style={{ margin: "6px 0 0", paddingLeft: 18 }}>{lastTick.problems.map((p, i) => <li key={i}>{p}</li>)}</ul>
              </div>
            ) : null}
          </Card>
        </div>
        <SettingsPanel
          settings={JSON.parse(JSON.stringify(settings))}
          recipients={JSON.parse(JSON.stringify(people))}
          ledger={JSON.parse(JSON.stringify(rows.map((r) => ({
            delivery_id: r.delivery_id, to: r.to_address, purpose: r.purpose, status: r.status,
            status_reason: r.status_reason, attempts: r.attempts, created_at: r.created_at, edition: r.edition_label,
            last_error: r.last_error, last_event: r.last_event,
          }))))}
          sectors={[...SECTORS]}
          canEmail={Boolean(who.email)}
        />
      </div>
    </>
  );
}
