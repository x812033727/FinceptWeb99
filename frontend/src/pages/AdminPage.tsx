import { useRef, useState } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { useVirtualizer } from "@tanstack/react-virtual";
import { useNavigate, useSearchParams } from "react-router-dom";
import { useTranslation } from "react-i18next";
import { useAuthStore } from "@/store/authStore";
import api from "@/lib/api";
import { CollapsibleHeader } from "@/components/Collapsible";
import { useCollapsible } from "@/hooks/useCollapsible";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import FinmindAdminCard from "@/components/admin/FinmindAdminCard";
import FinmindBackfillCard from "@/components/admin/FinmindBackfillCard";
import FinmindKeysCard from "@/components/admin/FinmindKeysCard";
import FinmindUsageCard from "@/components/admin/FinmindUsageCard";
import { IngestHealthCard } from "@/components/admin/IngestHealthCard";
import { LLMKeysCard } from "@/components/admin/LLMKeysCard";
import { MarketKeysCard } from "@/components/admin/MarketKeysCard";
import { PersonasCard } from "@/components/admin/PersonasCard";
import { RuntimeTunablesCard } from "@/components/admin/RuntimeTunablesCard";
import { DiscussionLessonsCard } from "@/components/admin/DiscussionLessonsCard";
import { CalibrationReviewCard } from "@/components/admin/CalibrationReviewCard";
import { LessonArchiveCard } from "@/components/admin/LessonArchiveCard";
import { PostMortemGapsCard } from "@/components/admin/PostMortemGapsCard";
import { SignalAuditCard } from "@/components/admin/SignalAuditCard";
import { SignalQualityCard } from "@/components/admin/SignalQualityCard";
import { SystemTasksCard } from "@/components/admin/SystemTasksCard";
import { SystemUpdateCard } from "@/components/admin/SystemUpdateCard";
import { RedeployCard } from "@/components/admin/RedeployCard";
import { UsageCard } from "@/components/admin/UsageCard";
import { DbBrowserTab } from "@/components/admin/DbBrowserTab";

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
  admin: "border-warning/30 text-warning bg-warning/10",
};

// Shared grid template for the users header row and virtualized data
// rows — keep the two identical or columns drift out of alignment.
const USERS_GRID_COLS =
  "grid grid-cols-[minmax(0,1fr)_90px_70px_90px_70px]";

const TAB_KEYS = ["ops", "ai", "data", "quality", "users", "usage", "db"] as const;
type TabKey = (typeof TAB_KEYS)[number];
const DEFAULT_TAB: TabKey = "ops";

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
  const { t } = useTranslation();
  const [searchParams, setSearchParams] = useSearchParams();
  const tabParam = searchParams.get("tab") as TabKey | null;
  const activeTab: TabKey = tabParam && TAB_KEYS.includes(tabParam) ? tabParam : DEFAULT_TAB;

  function setActiveTab(next: string) {
    const nextParams = new URLSearchParams(searchParams);
    nextParams.set("tab", next);
    setSearchParams(nextParams, { replace: true });
  }

  return (
    <div className="p-4 sm:p-6 space-y-5 max-w-5xl">
      <h1 className="text-xl font-semibold">{t("admin.title")}</h1>

      <Tabs value={activeTab} onValueChange={setActiveTab} className="space-y-4">
        {/* md+ — full tab strip. */}
        <TabsList className="hidden md:inline-flex h-auto flex-wrap">
          {TAB_KEYS.map((k) => (
            <TabsTrigger key={k} value={k}>
              {t(`admin.tab.${k}`)}
            </TabsTrigger>
          ))}
        </TabsList>

        {/* <md — tab list overflows on narrow screens, so we surface a
            Select dropdown instead. URL state is the single source of
            truth, so both controls stay in sync. */}
        <div className="md:hidden">
          <Select value={activeTab} onValueChange={setActiveTab}>
            <SelectTrigger>
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              {TAB_KEYS.map((k) => (
                <SelectItem key={k} value={k}>
                  {t(`admin.tab.${k}`)}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </div>

        <TabsContent value="ops" className="space-y-4 mt-4">
          <RedeployCard />
          <SystemUpdateCard />
          <IngestHealthCard />
          <RuntimeTunablesCard />
        </TabsContent>

        <TabsContent value="ai" className="space-y-4 mt-4">
          <LLMKeysCard />
          <SystemTasksCard />
          <PersonasCard />
        </TabsContent>

        <TabsContent value="data" className="space-y-4 mt-4">
          <MarketKeysCard />
          <FinmindBackfillCard />
          <FinmindAdminCard />
          <FinmindKeysCard />
        </TabsContent>

        <TabsContent value="quality" className="space-y-4 mt-4">
          <CalibrationReviewCard />
          <SignalAuditCard />
          <SignalQualityCard />
          <PostMortemGapsCard />
          <DiscussionLessonsCard />
          <LessonArchiveCard />
        </TabsContent>

        <TabsContent value="users" className="space-y-4 mt-4">
          <UsersTab />
        </TabsContent>

        <TabsContent value="usage" className="space-y-4 mt-4">
          <UsageCard scope="admin" />
          <FinmindUsageCard />
        </TabsContent>

        <TabsContent value="db" className="space-y-4 mt-4">
          <DbBrowserTab />
        </TabsContent>
      </Tabs>
    </div>
  );
}

function UsersTab() {
  const { t } = useTranslation();
  const qc = useQueryClient();
  const [editingId, setEditingId] = useState<string | null>(null);

  const { data: stats } = useQuery<SystemStats>({
    queryKey: ["admin", "stats"],
    queryFn: () => api.get("/admin/stats").then((r) => r.data),
  });

  const { data: users = [], isLoading } = useQuery<AdminUser[]>({
    queryKey: ["admin", "users"],
    queryFn: () => api.get("/admin/users?limit=200").then((r) => r.data),
  });

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
    <div className="space-y-4">
      {stats && (
        <div className="grid grid-cols-2 gap-3 sm:grid-cols-5">
          {[
            { label: t("admin.stats.total_users"), value: stats.total_users },
            { label: t("admin.stats.active"), value: stats.active_users },
            { label: t("admin.stats.viewers"), value: stats.users_by_role.viewer ?? 0 },
            { label: t("admin.stats.alerts"), value: stats.total_alerts },
            { label: t("admin.stats.watchlists"), value: stats.total_watchlists },
          ].map(({ label, value }) => (
            <div key={label} className="bg-card border border-border rounded-lg p-3 text-center">
              <p className="text-2xl font-bold tabular-nums">{value}</p>
              <p className="text-xs text-muted-foreground mt-0.5">{label}</p>
            </div>
          ))}
        </div>
      )}

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

function UsersSection({
  users,
  isLoading,
  editingId,
  setEditingId,
  updateRole,
  toggleActive,
}: {
  users: AdminUser[];
  isLoading: boolean;
  editingId: string | null;
  setEditingId: (id: string | null) => void;
  updateRole: ReturnType<typeof useMutation<unknown, Error, { id: string; role: string }>>;
  toggleActive: ReturnType<typeof useMutation<unknown, Error, { id: string; is_active: boolean }>>;
}) {
  const { open, toggle } = useCollapsible("admin.users");
  // Virtualized rows (PR-9) — the endpoint returns up to 200 users;
  // only the visible window is mounted (ScreenerPage pattern: header
  // row outside the scroll element, absolute rows in a relative
  // spacer). Short lists stay scrollbar-free — max-height cap only.
  const parentRef = useRef<HTMLDivElement>(null);
  const rowVirtualizer = useVirtualizer({
    count: users.length,
    getScrollElement: () => parentRef.current,
    estimateSize: () => 44,
    overscan: 10,
  });

  return (
    <div className="bg-card border border-border rounded-lg p-4 space-y-3">
      <CollapsibleHeader open={open} toggle={toggle} title={`Users (${users.length})`} />
      {open && (
        <div className="-mx-4 -mb-4 overflow-hidden rounded-b-lg">
          {isLoading ? (
            <p className="p-4 text-xs text-muted-foreground animate-pulse">Loading…</p>
          ) : (
            <div className="text-sm">
              <div className={`${USERS_GRID_COLS} gap-x-2 border-b border-border px-4 py-2.5`}>
                {["Email", "Role", "Status", "Joined", "Actions"].map((h) => (
                  <span key={h} className="text-left text-xs font-medium text-muted-foreground">
                    {h}
                  </span>
                ))}
              </div>
              <div ref={parentRef} className="overflow-y-auto max-h-[60vh]" data-testid="admin-users-virtual-scroll">
                <div style={{ height: rowVirtualizer.getTotalSize(), position: "relative" }}>
                  {rowVirtualizer.getVirtualItems().map((virtualRow) => {
                    const u = users[virtualRow.index];
                    return (
                      <div
                        key={u.id}
                        data-index={virtualRow.index}
                        ref={rowVirtualizer.measureElement}
                        className={`${USERS_GRID_COLS} gap-x-2 absolute w-full px-4 items-center border-b border-border hover:bg-accent/5`}
                        style={{ top: virtualRow.start, height: 44 }}
                      >
                        <span className="text-xs truncate">{u.email}</span>
                        <span>
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
                        </span>
                        <span
                          className={`text-xs ${
                            u.is_active ? "text-success" : "text-danger"
                          }`}
                        >
                          {u.is_active ? "active" : "disabled"}
                        </span>
                        <span className="text-xs text-muted-foreground">
                          {new Date(u.created_at).toLocaleDateString()}
                        </span>
                        <span>
                          <button
                            onClick={() =>
                              toggleActive.mutate({ id: u.id, is_active: !u.is_active })
                            }
                            className="text-xs text-muted-foreground hover:text-foreground transition-colors min-h-[28px]"
                          >
                            {u.is_active ? "Disable" : "Enable"}
                          </button>
                        </span>
                      </div>
                    );
                  })}
                </div>
              </div>
            </div>
          )}
        </div>
      )}
    </div>
  );
}
