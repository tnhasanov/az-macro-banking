/**
 * The page an unsubscribe link opens. Visiting it changes nothing — mail scanners open links —
 * and the button posts the signed token back.
 */
import { UnsubscribeButton } from "@/components/UnsubscribeButton";

export const dynamic = "force-dynamic";

export default async function UnsubscribePage({ searchParams }: { searchParams: Promise<{ t?: string }> }) {
  const { t } = await searchParams;
  return (
    <main style={{ maxWidth: 520, margin: "80px auto", padding: "0 16px" }}>
      <h1 style={{ fontSize: 20 }}>Stop report announcements</h1>
      <p style={{ fontSize: 14 }}>You will no longer be emailed when a new edition is published. Reports you ask for
        yourself are not affected.</p>
      {t ? <UnsubscribeButton token={t} /> : <p className="error-text">This link is incomplete.</p>}
    </main>
  );
}
