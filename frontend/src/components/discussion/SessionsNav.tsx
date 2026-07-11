/**
 * Sessions navigation for DiscussionPage — extracted verbatim from
 * `pages/DiscussionPage.tsx` (PR-8 巨石頁拆分):
 *
 * - `SessionsFilterBar`  — search box + status pills. The filter STATE
 *   stays in the page because desktop rail and mobile drawer share it.
 * - `SessionsRail`       — desktop sidebar: filter on top + virtualized
 *   sessions list below (+ back-to-dashboard footer).
 * - `SessionsDrawerContent` — mobile Sheet body: tool cards +
 *   new-discussion button + filter + non-virtualized list + footer.
 */
import { useRef } from "react";
import { useTranslation } from "react-i18next";
import { useVirtualizer } from "@tanstack/react-virtual";
import { Search } from "lucide-react";
import { cn } from "@/lib/utils";
import type { AgentInfo, Discussion, DiscussionMarket } from "@/types/discussion";
import { AutoRunConfigCard } from "@/components/discussion/AutoRunConfigCard";
import { BacktestSweepCard } from "@/components/discussion/BacktestSweepCard";
import { StrategyTemplateCard } from "@/components/discussion/StrategyTemplateCard";
import { SessionRowBody } from "@/components/discussion/SessionRowBody";
import { formatDiscussionTitle } from "@/components/discussion/_helpers";
import type { CollapseState } from "@/components/discussion/_helpers";

export type SessionsStatusFilter = "all" | "draft" | "running" | "done";

export function SessionsFilterBar({
  sessions,
  sessionsQuery,
  setSessionsQuery,
  sessionsStatusFilter,
  setSessionsStatusFilter,
}: {
  sessions: Discussion[];
  sessionsQuery: string;
  setSessionsQuery: (q: string) => void;
  sessionsStatusFilter: SessionsStatusFilter;
  setSessionsStatusFilter: (s: SessionsStatusFilter) => void;
}) {
  const { t } = useTranslation();
  if (sessions.length === 0) return null;
  return (
    <div className="space-y-1.5">
      <div className="relative">
        <Search className="absolute left-2 top-1/2 -translate-y-1/2 h-3.5 w-3.5 text-muted-foreground pointer-events-none" aria-hidden="true" />
        <input
          type="search"
          value={sessionsQuery}
          onChange={(e) => setSessionsQuery(e.target.value)}
          placeholder={t("discussion.sessions_search_placeholder")}
          aria-label={t("discussion.sessions_search_placeholder")}
          className="w-full pl-7 pr-2 py-1.5 rounded-md bg-card border border-border text-xs text-foreground focus:outline-none focus:border-primary/50 min-h-[32px]"
        />
      </div>
      <div className="flex gap-1 flex-wrap">
        {(["all", "draft", "running", "done"] as const).map((s) => (
          <button
            key={s}
            type="button"
            onClick={() => setSessionsStatusFilter(s)}
            className={cn(
              "px-2 py-0.5 rounded-full text-[10px] border transition-colors min-h-[24px]",
              sessionsStatusFilter === s
                ? "border-primary bg-primary/15 text-primary"
                : "border-border text-muted-foreground hover:text-foreground"
            )}
          >
            {t(`discussion.sessions_filter.${s}`)}
          </button>
        ))}
      </div>
    </div>
  );
}

export function SessionsRail({
  sessions,
  filteredSessions,
  selectedId,
  setSelectedId,
  sessionsQuery,
  setSessionsQuery,
  sessionsStatusFilter,
  setSessionsStatusFilter,
}: {
  sessions: Discussion[];
  filteredSessions: Discussion[];
  selectedId: string | null;
  setSelectedId: (id: string) => void;
  sessionsQuery: string;
  setSessionsQuery: (q: string) => void;
  sessionsStatusFilter: SessionsStatusFilter;
  setSessionsStatusFilter: (s: SessionsStatusFilter) => void;
}) {
  const { t } = useTranslation();
  // Virtualization parent for the desktop sessions rail. Keeps the
  // DOM small (~30 rows) regardless of how many discussions the user
  // has accumulated; without this, a 200-row archive renders 200
  // backtest scoreboard cells on every render.
  const sessionsScrollRef = useRef<HTMLDivElement>(null);

  // Desktop virtualizer. `estimateSize` returns a taller estimate
  // for backtest sessions (whose row contains a date + N symbol
  // scoreboards) so the parent total height isn't off by 50%
  // when most rows are backtest. Virtualizer reconciles actual
  // measured heights post-mount via `measureElement`.
  const sessionsVirtualizer = useVirtualizer({
    count: filteredSessions.length,
    getScrollElement: () => sessionsScrollRef.current,
    estimateSize: (i) => {
      const s = filteredSessions[i];
      if (!s) return 56;
      const tt = formatDiscussionTitle(s);
      if (tt.text !== undefined) return 56;
      return 56 + (tt.lines?.length ?? 0) * 18;
    },
    overscan: 8,
  });

  function renderSessionsListVirtualized() {
    if (sessions.length === 0) {
      return (
        <p className="text-xs text-muted-foreground">{t("discussion.empty")}</p>
      );
    }
    if (filteredSessions.length === 0) {
      return (
        <p className="text-xs text-muted-foreground">
          {t("discussion.sessions_filter_empty")}
        </p>
      );
    }
    return (
      <div
        style={{
          height: sessionsVirtualizer.getTotalSize(),
          position: "relative",
          width: "100%",
        }}
      >
        {sessionsVirtualizer.getVirtualItems().map((vi) => {
          const s = filteredSessions[vi.index];
          if (!s) return null;
          return (
            <button
              key={s.id}
              data-index={vi.index}
              ref={sessionsVirtualizer.measureElement}
              onClick={() => setSelectedId(s.id)}
              className={cn(
                "absolute left-0 right-0 text-left px-2 py-2 rounded text-xs transition-colors",
                selectedId === s.id
                  ? "bg-primary/15 text-primary"
                  : "hover:bg-accent/10 text-muted-foreground"
              )}
              style={{
                transform: `translateY(${vi.start}px)`,
              }}
            >
              <SessionRowBody s={s} />
            </button>
          );
        })}
      </div>
    );
  }

  return (
    /* Desktop sidebar — always visible at lg+. PR-E redesign:
        tools (AutoRun / Strategy / Sweep / + 新討論) moved to
        the horizontal toolbar at the top of the main column.
        The rail now holds ONLY sessions navigation: filter chips
        on top + virtualized list below. Mobile keeps the merged
        tools-plus-sessions drawer via the Sheet path. */
    <aside className="hidden lg:flex lg:w-60 border-r border-border flex-col shrink-0">
      <div className="p-3 shrink-0 border-b border-border">
        <SessionsFilterBar
          sessions={sessions}
          sessionsQuery={sessionsQuery}
          setSessionsQuery={setSessionsQuery}
          sessionsStatusFilter={sessionsStatusFilter}
          setSessionsStatusFilter={setSessionsStatusFilter}
        />
      </div>
      <div
        ref={sessionsScrollRef}
        className="flex-1 overflow-y-auto p-2"
      >
        {renderSessionsListVirtualized()}
      </div>
      <div className="border-t border-border px-3 py-2 text-xs text-muted-foreground shrink-0">
        <a href="/dashboard" className="hover:text-foreground transition-colors">
          {t("ai.back_dashboard")}
        </a>
      </div>
    </aside>
  );
}

// Mobile drawer body: tools + filter + list as one scroll. Desktop
// splits these into the toolbar (tools) and sidebar (filter + list).
export function SessionsDrawerContent({
  agents,
  collapse,
  toggleCollapse,
  personaName,
  topic,
  rules,
  market,
  personaIds,
  handleNewDiscussion,
  sessions,
  filteredSessions,
  selectedId,
  setSelectedId,
  setSessionsSheetOpen,
  sessionsQuery,
  setSessionsQuery,
  sessionsStatusFilter,
  setSessionsStatusFilter,
}: {
  agents: AgentInfo[];
  collapse: CollapseState;
  toggleCollapse: (key: keyof CollapseState) => void;
  personaName: (id: string) => string;
  topic: string;
  rules: string;
  market: DiscussionMarket;
  personaIds: string[];
  handleNewDiscussion: () => void;
  sessions: Discussion[];
  filteredSessions: Discussion[];
  selectedId: string | null;
  setSelectedId: (id: string) => void;
  setSessionsSheetOpen: (open: boolean) => void;
  sessionsQuery: string;
  setSessionsQuery: (q: string) => void;
  sessionsStatusFilter: SessionsStatusFilter;
  setSessionsStatusFilter: (s: SessionsStatusFilter) => void;
}) {
  const { t } = useTranslation();

  // Mobile sidebar drawer — keeps the original layout (header + 3
  // tool cards + new-discussion button + sessions filter + list).
  // Desktop uses `SessionsRail` only and surfaces tools via
  // the horizontal toolbar above the transcript.
  function renderSidebarTools() {
    return (
      <>
        <div>
          <h2 className="text-sm font-semibold text-foreground">{t("discussion.title")}</h2>
          <p className="text-xs text-muted-foreground mt-0.5">{t("discussion.subtitle")}</p>
        </div>

        <AutoRunConfigCard
          agents={agents}
          collapsed={collapse.autoRun}
          onToggleCollapse={() => toggleCollapse("autoRun")}
          personaName={personaName}
        />

        <StrategyTemplateCard
          prefill={{
            topic,
            rules,
            market,
            personaIdsCsv: personaIds.join(", "),
          }}
        />

        <BacktestSweepCard
          topic={topic}
          rules={rules}
          market={market}
          personaIds={personaIds}
        />

        <button
          onClick={handleNewDiscussion}
          className="px-3 py-2 rounded-md border border-border text-xs hover:border-primary/40 hover:bg-accent/10 transition-colors text-left min-h-[36px]"
        >
          + {t("discussion.new")}
        </button>
      </>
    );
  }

  function renderSessionsListNonVirtualized() {
    return (
      <div className="space-y-1">
        {sessions.length === 0 ? (
          <p className="text-xs text-muted-foreground">{t("discussion.empty")}</p>
        ) : filteredSessions.length === 0 ? (
          <p className="text-xs text-muted-foreground">
            {t("discussion.sessions_filter_empty")}
          </p>
        ) : null}
        {filteredSessions.map((s) => (
          <button
            key={s.id}
            onClick={() => {
              setSelectedId(s.id);
              setSessionsSheetOpen(false);
            }}
            className={cn(
              "w-full text-left px-2 py-2 rounded text-xs transition-colors min-h-[44px]",
              selectedId === s.id
                ? "bg-primary/15 text-primary"
                : "hover:bg-accent/10 text-muted-foreground"
            )}
          >
            <SessionRowBody s={s} />
          </button>
        ))}
      </div>
    );
  }

  return (
    <>
      {renderSidebarTools()}
      <SessionsFilterBar
        sessions={sessions}
        sessionsQuery={sessionsQuery}
        setSessionsQuery={setSessionsQuery}
        sessionsStatusFilter={sessionsStatusFilter}
        setSessionsStatusFilter={setSessionsStatusFilter}
      />
      {renderSessionsListNonVirtualized()}
      <div className="mt-auto text-xs text-muted-foreground pt-2">
        <a href="/dashboard" className="hover:text-foreground transition-colors">
          {t("ai.back_dashboard")}
        </a>
      </div>
    </>
  );
}
