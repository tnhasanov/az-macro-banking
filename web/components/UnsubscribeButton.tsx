"use client";

import { useState } from "react";

export function UnsubscribeButton({ token }: { token: string }) {
  const [state, setState] = useState<"idle" | "busy" | "done" | "error">("idle");
  const [message, setMessage] = useState("");
  async function go() {
    setState("busy");
    const response = await fetch(`/api/unsubscribe?t=${encodeURIComponent(token)}`, { method: "POST" });
    const data = await response.json().catch(() => ({}));
    setState(response.ok ? "done" : "error");
    setMessage((data as { note?: string; error?: string }).note ?? (data as { error?: string }).error ?? "");
  }
  if (state === "done") return <p className="ok-text">{message}</p>;
  return (
    <div>
      <button type="button" className="primary" onClick={() => void go()} disabled={state === "busy"}>Unsubscribe</button>
      {state === "error" && <p className="error-text">{message}</p>}
    </div>
  );
}
