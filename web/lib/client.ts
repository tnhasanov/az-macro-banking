/** Browser-side request helper: JSON in, JSON out, and the server's message on failure. */
export async function postJSON<T = Record<string, unknown>>(url: string, body: unknown = {}): Promise<T> {
  const response = await fetch(url, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(body),
    credentials: "same-origin",
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    const error = new Error((data as { error?: string }).error ?? `The server answered ${response.status}.`);
    (error as Error & { code?: string }).code = (data as { code?: string }).code;
    throw error;
  }
  return data as T;
}
