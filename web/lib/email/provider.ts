/**
 * Email providers behind one small interface.
 *
 * No provider was working before this: the engine's Microsoft Graph channel needs a Microsoft 365
 * tenant and an app registration that were never set up. So there is one real provider, Resend,
 * used through its official SDK, and a capture provider that writes messages to a directory for
 * local integration tests. A provider answers each send with one of three outcomes, because an
 * email API has three: accepted (it has the message), rejected (it does not, and says why), or
 * uncertain (the request may or may not have landed — a timeout, a dropped connection).
 */
import { mkdir, writeFile } from "node:fs/promises";
import path from "node:path";
import { environment } from "../appstate";

export interface OutgoingEmail {
  from: string;
  to: string;
  subject: string;
  text: string;
  html: string;
  headers?: Record<string, string>;
  attachment?: { filename: string; content: Buffer } | null;
  tags?: { name: string; value: string }[];
}

export type SendResult =
  | { kind: "accepted"; providerMessageId: string }
  | { kind: "rejected"; permanent: boolean; code: string; message: string; retryAfterSeconds?: number }
  | { kind: "uncertain"; message: string };

export interface EmailProvider {
  name: string;
  send(message: OutgoingEmail, idempotencyKey: string): Promise<SendResult>;
}

/** Resend's error names, sorted by what a sender should do about them. */
const RETRY_LATER = new Set(["rate_limit_exceeded", "concurrent_idempotent_requests", "daily_quota_exceeded",
  "monthly_quota_exceeded"]);
const CONFIGURATION = new Set(["missing_api_key", "invalid_api_key", "restricted_api_key", "invalid_access",
  "invalid_from_address", "security_error", "invalid_region"]);

export function resendProvider(apiKey: string): EmailProvider {
  return {
    name: "resend",
    async send(message, idempotencyKey) {
      const { Resend } = await import("resend");
      const client = new Resend(apiKey);
      const { data, error } = await client.emails.send({
        from: message.from,
        to: message.to,
        subject: message.subject,
        text: message.text,
        html: message.html,
        headers: message.headers,
        tags: message.tags,
        attachments: message.attachment
          ? [{ filename: message.attachment.filename, content: message.attachment.content }]
          : undefined,
      }, { idempotencyKey });
      if (data?.id) return { kind: "accepted", providerMessageId: data.id };
      const name = error?.name ?? "application_error";
      const status = error?.statusCode ?? null;
      // The SDK turns a network failure into statusCode null: the request may have reached Resend.
      if (status === null) return { kind: "uncertain", message: error?.message ?? "no response" };
      if (status >= 500 || name === "internal_server_error" || name === "application_error") {
        return { kind: "uncertain", message: `Resend answered ${status} (${name})` };
      }
      if (RETRY_LATER.has(name)) {
        const hours = name.endsWith("quota_exceeded") ? 3600 : 60;
        return { kind: "rejected", permanent: false, code: name, message: error?.message ?? name, retryAfterSeconds: hours };
      }
      if (CONFIGURATION.has(name)) {
        // Not the message's fault, and not fixed by retrying in a minute: kept for retry once the
        // configuration is corrected, with a long delay so a broken key does not hammer the API.
        return { kind: "rejected", permanent: false, code: name, message: `${name}: ${error?.message ?? ""}`.trim(),
          retryAfterSeconds: 3600 };
      }
      return { kind: "rejected", permanent: true, code: name, message: error?.message ?? name };
    },
  };
}

/** Writes each message to a directory. Never available in production. */
export function captureProvider(dir: string): EmailProvider {
  return {
    name: "capture",
    async send(message, idempotencyKey) {
      await mkdir(dir, { recursive: true });
      const id = `cap_${idempotencyKey}`;
      await writeFile(path.join(dir, `${id}.json`), JSON.stringify({
        id, idempotency_key: idempotencyKey, ...message,
        attachment: message.attachment
          ? { filename: message.attachment.filename, bytes: message.attachment.content.length } : null,
      }, null, 2));
      return { kind: "accepted", providerMessageId: id };
    },
  };
}

export function emailProvider(): EmailProvider | { name: "none"; reason: string } {
  const env = environment();
  const choice = process.env.AZMONITOR_EMAIL_PROVIDER ?? (process.env.RESEND_API_KEY ? "resend" : "none");
  if (choice === "capture") {
    if (env === "production") return { name: "none", reason: "the capture provider is refused in production" };
    return captureProvider(process.env.AZMONITOR_EMAIL_CAPTURE_DIR ?? path.resolve(process.cwd(), "..", "data", "mail"));
  }
  if (choice === "resend") {
    if (!process.env.RESEND_API_KEY) return { name: "none", reason: "RESEND_API_KEY is not configured" };
    if (!process.env.AZMONITOR_EMAIL_FROM) return { name: "none", reason: "AZMONITOR_EMAIL_FROM is not configured" };
    return resendProvider(process.env.RESEND_API_KEY);
  }
  return { name: "none", reason: "no email provider is configured (set RESEND_API_KEY and AZMONITOR_EMAIL_FROM)" };
}

/**
 * Whether a message in this environment may leave the building. Only the production deployment
 * sends through a real provider; a preview or development deployment records what it would have
 * sent and sends nothing, whatever keys it happens to have.
 */
export function mayDeliver(provider: EmailProvider, rowEnvironment: string): { ok: boolean; reason?: string } {
  const env = environment();
  if (provider.name === "capture") return { ok: true };
  if (env !== "production") {
    return { ok: false, reason: `this is a ${env} deployment; only production sends email` };
  }
  if (rowEnvironment !== "production") {
    return { ok: false, reason: `the message belongs to the ${rowEnvironment} environment` };
  }
  return { ok: true };
}
