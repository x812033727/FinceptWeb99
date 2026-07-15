import { FormEvent, useState } from "react";
import { Link } from "react-router-dom";
import { forgotPassword } from "@/lib/auth";
import { AuthCard, Field, SubmitButton } from "./AcceptInvitePage";

export default function ForgotPasswordPage() {
  const [email, setEmail] = useState("");
  const [loading, setLoading] = useState(false);
  const [sent, setSent] = useState(false);
  async function submit(event: FormEvent) {
    event.preventDefault(); setLoading(true);
    try { await forgotPassword(email); setSent(true); } finally { setLoading(false); }
  }
  return <AuthCard title="Reset password" subtitle="Request a short-lived reset link">
    {sent ? <p role="status" className="text-sm text-foreground">If the account exists, reset instructions have been sent.</p> :
      <form onSubmit={submit} className="space-y-4"><Field label="Email" type="email" value={email} onChange={setEmail} /><SubmitButton loading={loading}>Send reset link</SubmitButton></form>}
    <Link to="/login" className="block text-center text-sm text-primary hover:underline">Back to sign in</Link>
  </AuthCard>;
}
