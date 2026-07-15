import { FormEvent, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { resetPassword } from "@/lib/auth";
import { AuthCard, Field, SubmitButton } from "./AcceptInvitePage";

export default function ResetPasswordPage() {
  const [params] = useSearchParams();
  const token = params.get("token") ?? "";
  const [password, setPassword] = useState("");
  const [loading, setLoading] = useState(false);
  const [done, setDone] = useState(false);
  const [error, setError] = useState<string | null>(null);
  async function submit(event: FormEvent) {
    event.preventDefault(); setLoading(true); setError(null);
    try { if (!token) throw new Error("Reset token is missing"); await resetPassword(token, password); setDone(true); }
    catch (err: any) { setError(err.response?.data?.detail ?? err.message); }
    finally { setLoading(false); }
  }
  return <AuthCard title="Choose a new password" subtitle="Reset links are single-use and expire shortly">
    {done ? <p role="status" className="text-sm">Password changed. You can now sign in.</p> :
      <form onSubmit={submit} className="space-y-4"><Field label="New password" type="password" value={password} onChange={setPassword} minLength={8} />{error && <p role="alert" className="text-negative text-sm">{error}</p>}<SubmitButton loading={loading}>Change password</SubmitButton></form>}
    <Link to="/login" className="block text-center text-sm text-primary hover:underline">Back to sign in</Link>
  </AuthCard>;
}
