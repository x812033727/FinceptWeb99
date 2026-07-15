import { FormEvent, useState } from "react";
import { useMutation } from "@tanstack/react-query";
import api, { errorDetail } from "@/lib/api";

interface InvitationCreated {
  id: string;
  email: string;
  role: "viewer" | "analyst" | "admin";
  expires_at: string;
  token: string;
}

function activationUrl(invitation: InvitationCreated): string {
  const query = new URLSearchParams({
    token: invitation.token,
    email: invitation.email,
  });
  return `${window.location.origin}/accept-invite?${query.toString()}`;
}

export default function InviteUserCard() {
  const [email, setEmail] = useState("");
  const [role, setRole] = useState<InvitationCreated["role"]>("analyst");
  const [expiresHours, setExpiresHours] = useState(48);
  const [copied, setCopied] = useState(false);

  const invite = useMutation({
    mutationFn: () =>
      api.post<InvitationCreated>("/admin/invitations", {
        email,
        role,
        expires_hours: expiresHours,
      }).then((response) => response.data),
    onSuccess: () => setCopied(false),
  });

  function submit(event: FormEvent) {
    event.preventDefault();
    invite.mutate();
  }

  const link = invite.data ? activationUrl(invite.data) : "";

  return (
    <section className="bg-card shadow-highlight border border-border rounded-lg p-4 space-y-3">
      <div>
        <h2 className="text-sm font-semibold text-foreground">Invite a user</h2>
        <p className="text-xs text-muted-foreground mt-0.5">
          The activation link is email-bound, single-use, and shown only in this browser session.
        </p>
      </div>

      <form onSubmit={submit} className="grid gap-3 sm:grid-cols-[minmax(0,1fr)_120px_120px_auto] sm:items-end">
        <label className="space-y-1 text-xs text-muted-foreground">
          <span>Email</span>
          <input
            type="email"
            required
            value={email}
            onChange={(event) => setEmail(event.target.value)}
            className="w-full rounded border border-border bg-background px-3 py-2 text-sm text-foreground"
          />
        </label>
        <label className="space-y-1 text-xs text-muted-foreground">
          <span>Role</span>
          <select
            value={role}
            onChange={(event) => setRole(event.target.value as InvitationCreated["role"])}
            className="w-full rounded border border-border bg-background px-3 py-2 text-sm text-foreground"
          >
            <option value="viewer">viewer</option>
            <option value="analyst">analyst</option>
            <option value="admin">admin</option>
          </select>
        </label>
        <label className="space-y-1 text-xs text-muted-foreground">
          <span>Expires in</span>
          <select
            value={expiresHours}
            onChange={(event) => setExpiresHours(Number(event.target.value))}
            className="w-full rounded border border-border bg-background px-3 py-2 text-sm text-foreground"
          >
            <option value={24}>24 hours</option>
            <option value={48}>48 hours</option>
            <option value={168}>7 days</option>
          </select>
        </label>
        <button
          type="submit"
          disabled={invite.isPending}
          className="rounded bg-primary px-4 py-2 text-sm text-primary-foreground disabled:opacity-50"
        >
          {invite.isPending ? "Creating…" : "Create invitation"}
        </button>
      </form>

      {invite.isError && (
        <p role="alert" className="text-xs text-danger">
          {errorDetail(invite.error)}
        </p>
      )}

      {invite.data && (
        <div role="status" className="rounded border border-success/30 bg-success/5 p-3 space-y-2">
          <p className="text-xs text-success">
            Invitation created for {invite.data.email}; expires {new Date(invite.data.expires_at).toLocaleString()}.
          </p>
          <div className="flex flex-col gap-2 sm:flex-row">
            <label className="sr-only" htmlFor="activation-link">Activation link</label>
            <input
              id="activation-link"
              aria-label="Activation link"
              readOnly
              value={link}
              className="min-w-0 flex-1 rounded border border-border bg-background px-3 py-2 text-xs text-foreground"
            />
            <button
              type="button"
              onClick={async () => {
                await navigator.clipboard.writeText(link);
                setCopied(true);
              }}
              className="rounded border border-border px-3 py-2 text-xs text-foreground hover:bg-accent/20"
            >
              {copied ? "Copied" : "Copy link"}
            </button>
          </div>
          <p className="text-micro text-warning">
            Copy it now. The raw token is not stored and cannot be displayed again.
          </p>
        </div>
      )}
    </section>
  );
}
