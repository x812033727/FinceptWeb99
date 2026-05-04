import { useState } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { useNavigate } from "react-router-dom";
import { useAuthStore } from "@/store/authStore";
import api from "@/lib/api";
import { CollapsibleHeader, useCollapsible } from "@/components/Collapsible";
import FinmindAdminCard from "@/components/admin/FinmindAdminCard";
import FinmindKeysCard from "@/components/admin/FinmindKeysCard";
import FinmindUsageCard from "@/components/admin/FinmindUsageCard";
import { IngestHealthCard } from "@/components/admin/IngestHealthCard";
import { LLMKeysCard } from "@/components/admin/LLMKeysCard";
import { MarketKeysCard } from "@/components/admin/MarketKeysCard";
import { PersonasCard } from "@/components/admin/PersonasCard";
import { RuntimeTunablesCard } from "@/components/admin/RuntimeTunablesCard";
import { PostMortemGapsCard } from "@/components/admin/PostMortemGapsCard";
import { SignalAuditCard } from "@/components/admin/SignalAuditCard";
import { SignalQualityCard } from "@/components/admin/SignalQualityCard";
import { SystemTasksCard } from "@/components/admin/SystemTasksCard";
import { SystemUpdateCard } from "@/components/admin/SystemUpdateCard";
import { UsageCard } from "@/components/admin/UsageCard";

interface AdminUser {
  id: string;
  email: string;
  role: "viewer" | "analyst" | "admin";
  is_active: boolean;
  created_at: string;
}

interface SystemStats {
  total_users: number;
  active_users: number;
  users_by_role: Record<string, number>;
  total_alerts: number;
  total_watchlists: number;
}

const ROLE_COLORS: Record<string, string> = {
  viewer: "border-muted-foreground/30 text-muted-foreground",
  analyst: "border-blue-400/30 text-blue-400 bg-blue-400/10",
  admin: "border-amber-400/30 text-amber-400 bg-amber-400/10",
};

export default function AdminPage() {
  const user = useAuthStore((s) => s.user);
  const navigate = useNavigate();

  if (user?.role !== "admin") {
    navigate("/dashboard", { replace: true });
    return null;
  }

  return <AdminContent />;
}

function AdminContent() {
  const qc = useQueryClient();

  const { data: stats } = useQuery<SystemStats>({
    queryKey: ["admin", "stats"],
    queryFn: () => api.get("/admin/stats").then((r) => r.data),
  });

  const { data: users = [], isLoading } = useQuery<AdminUser[]>({
    queryKey: ["admin", "users"],
    queryFn: () => api.get("/admin/users?limit=200").then((r) => r.data),
  });

  const [editingId, setEditingId] = useState<string | null>(null);

  const updateRole = useMutation({
    mutationFn: ({ id, role }: { id: string; role: string }) =>
      api.patch(`/admin/users/${id}/role`, { role }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["admin"] });
      setEditingId(null);
    },
  });

  const toggleActive = useMutation({
    mutationFn: ({ id, is_active }: { id: string; is_active: boolean }) =>
      api.patch(`/admin/users/${id}/active`, { is_active }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["admin"] }),
  });

  return (
    <div className="p-6 space-y-6 max-w-5xl">
      <h1 className="text-xl font-semibold">Admin</h1>

      {/* Stats */}
      {stats && (
        <div className="grid grid-cols-2 gap-3 sm:grid-cols-5">
          {[
            { label: "Total Users", value: stats.total_users },
            { label: "Active", value: stats.active_users },
            { label: "Viewers", value: stats.users_by_role.viewer ?? 0 },
            { label: "Alerts", value: stats.total_alerts },
            { label: "Watchlists", value: stats.total_watchlists },
          ].map(({ label, value }) => (
            <div
              key={label}
              className="bg-card border border-border rounded-lg p-3 text-center"
            >
              <p className="text-2xl font-bold tabular-nums">{value}</p>
              <p className="text-xs text-muted-foreground mt-0.5">{label}</p>
            </div>
          ))}
        </div>
      )}

      <SystemUpdateCard />

      <LLMKeysCard />

      <MarketKeysCard />

      <UsageCard scope="admin" />

      <IngestHealthCard />

      <PersonasCard />

      <SystemTasksCard />

      <RuntimeTunablesCard />

      <SignalAuditCard />

      <SignalQualityCard />

      <PostMortemGapsCard />

      <FinmindAdminCard />

      <FinmindUsageCard />

      <FinmindKeysCard />

      <UsersSection
        users={users}
        isLoading={isLoading}
        editingId={editingId}
        setEditingId={setEditingId}
        updateRole={updateRole}
        toggleActive={toggleActive}
      />
    </div>
  );
}

// Inline collapsible wrapper for the Users table — kept as a child
// component (not refactored fully out of AdminContent) so the
// mutation callbacks + state handlers stay where they were defined.
function UsersSection({
  users, isLoading, editingId, setEditingId, updateRole, toggleActive,
}: {
  users: AdminUser[];
  isLoading: boolean;
  editingId: string | null;
  setEditingId: (id: string | null) => void;
  updateRole: any;
  toggleActive: any;
}) {
  const { open, toggle } = useCollapsible("admin.users");
  return (
    <div className="bg-card border border-border rounded-lg p-4 space-y-3">
      <CollapsibleHeader
        open={open} toggle={toggle}
        title={`Users (${users.length})`}
      />
      {open && (<div className="-mx-4 -mb-4 overflow-hidden rounded-b-lg">
        {isLoading ? (
          <p className="p-4 text-xs text-muted-foreground animate-pulse">Loading…</p>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b border-border">
                  {["Email", "Role", "Status", "Joined", "Actions"].map((h) => (
                    <th
                      key={h}
                      className="px-4 py-2.5 text-left text-xs font-medium text-muted-foreground"
                    >
                      {h}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody className="divide-y divide-border">
                {users.map((u) => (
                  <tr key={u.id} className="hover:bg-accent/5">
                    <td className="px-4 py-2.5 text-xs">{u.email}</td>
                    <td className="px-4 py-2.5">
                      {editingId === u.id ? (
                        <select
                          defaultValue={u.role}
                          className="bg-background border border-border rounded px-1.5 py-0.5 text-xs"
                          onChange={(e) =>
                            updateRole.mutate({ id: u.id, role: e.target.value })
                          }
                          onBlur={() => setEditingId(null)}
                          autoFocus
                        >
                          <option value="viewer">viewer</option>
                          <option value="analyst">analyst</option>
                          <option value="admin">admin</option>
                        </select>
                      ) : (
                        <button
                          onClick={() => setEditingId(u.id)}
                          className={`text-xs border rounded px-1.5 py-0.5 ${ROLE_COLORS[u.role]}`}
                          title="Click to change role"
                        >
                          {u.role}
                        </button>
                      )}
                    </td>
                    <td className="px-4 py-2.5">
                      <span
                        className={`text-xs ${
                          u.is_active ? "text-green-400" : "text-red-400"
                        }`}
                      >
                        {u.is_active ? "active" : "disabled"}
                      </span>
                    </td>
                    <td className="px-4 py-2.5 text-xs text-muted-foreground">
                      {new Date(u.created_at).toLocaleDateString()}
                    </td>
                    <td className="px-4 py-2.5">
                      <button
                        onClick={() =>
                          toggleActive.mutate({ id: u.id, is_active: !u.is_active })
                        }
                        className="text-xs text-muted-foreground hover:text-foreground transition-colors"
                      >
                        {u.is_active ? "Disable" : "Enable"}
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>)}
    </div>
  );
}
