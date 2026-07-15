import { useEffect, useState } from "react";
import api, { errorDetail } from "@/lib/api";

type Consent = {
  document: "terms" | "privacy" | "ai_data_disclosure";
  required_version: string;
  accepted: boolean;
  accepted_at: string | null;
};

const COPY: Record<Consent["document"], { title: string; body: string }> = {
  terms: { title: "Terms of use", body: "Fincept provides research and decision support only. It does not execute trades or provide personalised investment advice." },
  privacy: { title: "Privacy policy", body: "Account activity, saved research and portfolio data are stored to operate the invited Professional Beta." },
  ai_data_disclosure: { title: "AI and market-data disclosure", body: "AI output may be incomplete or incorrect. Market data can be delayed or unavailable; always verify evidence and timestamps before making a decision." },
};

export default function ConsentGate({ children }: { children: React.ReactNode }) {
  const [items, setItems] = useState<Consent[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    api.get<Consent[]>("/auth/consents").then((response) => setItems(response.data)).catch((err) => setError(errorDetail(err)));
  }, []);

  if (error) return <GateShell><p role="alert" className="text-negative">Unable to load required disclosures: {error}</p></GateShell>;
  if (!items) return <GateShell><p className="text-muted-foreground animate-pulse">Loading required disclosures…</p></GateShell>;
  const pending = items.filter((item) => !item.accepted);
  if (pending.length === 0) return <>{children}</>;

  async function acceptAll() {
    setSaving(true); setError(null);
    try {
      const accepted = await Promise.all(pending.map((item) => api.post<Consent>("/auth/consents", {
        document: item.document, version: item.required_version,
      })));
      const updates = new Map(accepted.map((response) => [response.data.document, response.data]));
      setItems((current) => current?.map((item) => updates.get(item.document) ?? item) ?? null);
    } catch (err) { setError(errorDetail(err)); }
    finally { setSaving(false); }
  }

  return <GateShell>
    <h1 className="text-xl font-semibold">Review required disclosures</h1>
    <p className="text-sm text-muted-foreground">You must accept the current versions before using the Professional Beta.</p>
    <div className="space-y-3">{pending.map((item) => <section key={item.document} className="rounded-md border border-border p-4">
      <h2 className="font-medium">{COPY[item.document].title}</h2>
      <p className="mt-1 text-sm text-muted-foreground">{COPY[item.document].body}</p>
      <p className="mt-2 text-xs text-muted-foreground">Version {item.required_version}</p>
    </section>)}</div>
    {error && <p role="alert" className="text-negative text-sm">{error}</p>}
    <button onClick={acceptAll} disabled={saving} className="w-full rounded-md bg-primary px-4 py-2 text-primary-foreground disabled:opacity-50">{saving ? "Saving…" : "Accept all and continue"}</button>
  </GateShell>;
}

function GateShell({ children }: { children: React.ReactNode }) {
  return <div className="min-h-screen flex items-center justify-center bg-background p-4"><main className="w-full max-w-xl space-y-5 rounded-lg border border-border bg-card p-6">{children}</main></div>;
}
