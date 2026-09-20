"use client";

import { Suspense, useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";

function LoginForm() {
  const router = useRouter();
  const params = useSearchParams();
  const [passphrase, setPassphrase] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const response = await fetch("/api/auth/login", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ passphrase }),
      });
      if (!response.ok) {
        const body = await response.json().catch(() => ({}));
        setError(body.error ?? "Sign-in failed.");
        return;
      }
      // `next` is only ever used as a path on this origin; an absolute URL would be an open
      // redirect, so anything that is not a plain path is discarded.
      const next = params.get("next");
      const target = next && /^\/(?!\/)/.test(next) ? next : "/";
      router.replace(target);
      router.refresh();
    } catch {
      setError("The dashboard could not be reached.");
    } finally {
      setBusy(false);
      setPassphrase("");
    }
  }

  return (
    <form onSubmit={submit} className="stack" style={{ gap: 16 }}>
      <div className="field">
        <label htmlFor="passphrase">Passphrase</label>
        <input
          id="passphrase"
          type="password"
          autoComplete="current-password"
          value={passphrase}
          onChange={(e) => setPassphrase(e.target.value)}
          required
          autoFocus
        />
      </div>
      {error && <div className="notice bad" role="alert">{error}</div>}
      <button type="submit" className="primary" disabled={busy || passphrase.length === 0}>
        {busy ? "Checking…" : "Sign in"}
      </button>
    </form>
  );
}

export default function LoginPage() {
  return (
    <main
      style={{
        minHeight: "100vh", display: "grid", placeItems: "center", padding: 24,
      }}
    >
      <div className="card" style={{ width: "100%", maxWidth: 380, padding: 26 }}>
        <h1 style={{ fontSize: 19, marginBottom: 4 }}>Macro &amp; Banking Monitor</h1>
        <p className="muted" style={{ fontSize: 13, margin: "0 0 20px" }}>
          This dashboard is private. Reports, source documents and run history are only available
          after signing in.
        </p>
        <Suspense fallback={null}>
          <LoginForm />
        </Suspense>
      </div>
    </main>
  );
}
