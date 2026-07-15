import { FormEvent, useState } from "react";
import { Link, useNavigate, useSearchParams } from "react-router-dom";

import { acceptInvite } from "@/lib/auth";

export default function AcceptInvitePage() {
  const [params] = useSearchParams();
  const navigate = useNavigate();
  const token = params.get("token") ?? "";
  const [email, setEmail] = useState(params.get("email") ?? "");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  async function submit(event: FormEvent) {
    event.preventDefault();
    setLoading(true);
    setError(null);
    try {
      if (!token) throw new Error("Invitation token is missing");
      await acceptInvite(token, email, password);
      navigate("/dashboard");
    } catch (err: any) {
      setError(err.response?.data?.detail ?? err.message ?? "Invitation could not be accepted");
    } finally {
      setLoading(false);
    }
  }

  return <AuthCard title="Accept invitation" subtitle="Create your Fincept Professional Beta account">
    <form onSubmit={submit} className="space-y-4">
      <Field label="Email" type="email" value={email} onChange={setEmail} />
      <Field label="Password" type="password" value={password} onChange={setPassword} minLength={8} />
      {error && <p role="alert" className="text-negative text-sm">{error}</p>}
      <SubmitButton loading={loading}>Accept invitation</SubmitButton>
    </form>
    <Link to="/login" className="block text-center text-sm text-primary hover:underline">Back to sign in</Link>
  </AuthCard>;
}

export function AuthCard({ title, subtitle, children }: { title: string; subtitle: string; children: React.ReactNode }) {
  return <div className="min-h-screen flex items-center justify-center bg-background px-4">
    <div className="w-full max-w-sm bg-card border border-border rounded-lg p-8 space-y-6">
      <div><h1 className="text-2xl font-bold text-primary">{title}</h1><p className="text-sm text-muted-foreground mt-1">{subtitle}</p></div>
      {children}
    </div>
  </div>;
}

export function Field({ label, type, value, onChange, minLength }: { label: string; type: string; value: string; onChange: (value: string) => void; minLength?: number }) {
  return <label className="block text-sm text-foreground">{label}
    <input aria-label={label} type={type} required minLength={minLength} value={value} onChange={(e) => onChange(e.target.value)} className="mt-1 w-full px-3 py-2 rounded-md bg-input border border-border text-foreground outline-none focus:ring-1 focus:ring-ring" />
  </label>;
}

export function SubmitButton({ loading, children }: { loading: boolean; children: React.ReactNode }) {
  return <button type="submit" disabled={loading} className="w-full py-2 px-4 rounded-md bg-primary text-primary-foreground text-sm font-medium disabled:opacity-50">{loading ? "Please wait…" : children}</button>;
}
