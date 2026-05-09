import { useMemo, useState, useRef, useEffect } from "react";
import { useQuery } from "@tanstack/react-query";
import { useLocation } from "react-router-dom";
import { useTranslation } from "react-i18next";
import { ChevronDown, ChevronRight, MoreHorizontal, Search, Users, Wrench, Trash2, Check } from "lucide-react";
import { useCollapsible } from "@/hooks/useCollapsible";
import api, { notifyRateLimited } from "@/lib/api";
import { useAuthStore } from "@/store/authStore";
import { cn } from "@/lib/utils";
import { MessageBubble } from "@/components/ai/MessageBubble";
import { ChatComposer } from "@/components/ai/ChatComposer";
import type { ChatMessage, ToolCallEvent } from "@/components/ai/types";
import { fetchAgents } from "@/components/discussion/_api";
import type { AgentInfo } from "@/types/discussion";
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
  SheetTrigger,
} from "@/components/ui/sheet";
import {
  DropdownMenu,
  DropdownMenuCheckboxItem,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";

// ── types ──────────────────────────────────────────────────────────

interface UserMe {
  ai_requests_remaining: number | null;
}

// ── api helpers ────────────────────────────────────────────────────

async function fetchMe(): Promise<UserMe> {
  const res = await api.get<UserMe>("/auth/me");
  return res.data;
}

// ── persona group config ───────────────────────────────────────────

const providerColor: Record<string, string> = {
  openai: "text-green-400",
  anthropic: "text-orange-400",
  gemini: "text-blue-400",
  ollama: "text-purple-400",
  claude_agent: "text-amber-300",
  groq: "text-pink-400",
  minimax: "text-cyan-400",
  deepseek: "text-indigo-400",
  openrouter: "text-rose-400",
};

// Grouping for the persona picker — keeps the 23 personas scannable.
// Source-of-truth lives in i18n keys (personas.groups.<id>.title / .hint)
// so the labels translate; only the membership map is structural.
// Unknown agent IDs fall through to a catch-all "其他" group so nothing
// silently disappears.
const PERSONA_GROUPS: { id: string; agentIds: string[] }[] = [
  { id: "functional", agentIds: ["market_analyst", "portfolio_advisor", "risk_manager", "macro_analyst", "earnings_analyst", "trading_coach", "claude_research"] },
  { id: "value",      agentIds: ["buffett", "graham", "munger"] },
  { id: "growth",     agentIds: ["lynch", "fisher", "smith"] },
  { id: "contrarian", agentIds: ["marks", "klarman"] },
  { id: "macro",      agentIds: ["dalio", "soros"] },
  { id: "quant",      agentIds: ["simons", "asness"] },
  { id: "short_term", agentIds: ["livermore", "ptj", "minervini", "raschke"] },
];

function AgentCard({
  agent,
  selected,
  onClick,
}: {
  agent: AgentInfo;
  selected: boolean;
  onClick: () => void;
}) {
  const { t, i18n } = useTranslation();
  const nameKey = `personas.agents.${agent.id}.name`;
  const descKey = `personas.agents.${agent.id}.description`;
  const localName = i18n.exists(nameKey) ? t(nameKey) : agent.name;
  const localDesc = i18n.exists(descKey) ? t(descKey) : agent.description;

  return (
    <button
      type="button"
      onClick={onClick}
      className={cn(
        "w-full text-left p-3 rounded-lg border transition-colors min-h-[44px]",
        selected
          ? "border-primary bg-primary/10"
          : "border-border bg-card hover:border-primary/40"
      )}
      aria-current={selected ? "true" : undefined}
    >
      <div className="flex items-start gap-2">
        <div className="flex-1 min-w-0">
          <div className="font-medium text-sm text-foreground">{localName}</div>
          <div className="text-xs text-muted-foreground mt-0.5 leading-snug">{localDesc}</div>
          <div className={cn("text-xs mt-1", providerColor[agent.default_provider] ?? "text-muted-foreground")}>
            {agent.default_provider}
          </div>
        </div>
        {selected && <Check className="h-4 w-4 text-primary shrink-0 mt-0.5" aria-hidden="true" />}
      </div>
    </button>
  );
}

// One persona group rendered with a clickable header that expands /
// collapses the group's AgentCards. Per-group collapse state persists
// to localStorage via `useCollapsible`, so a user who only ever uses
// quants can collapse value/growth/macro and stop scrolling past
// them on every visit. When `query` is non-empty the group ignores
// its persisted collapse state and stays open so search results are
// always visible.
function PersonaGroupBlock({
  groupId,
  agents,
  selectedAgent,
  onPick,
  forceOpen,
}: {
  groupId: string;
  agents: AgentInfo[];
  selectedAgent: string;
  onPick: (id: string) => void;
  forceOpen: boolean;
}) {
  const { t } = useTranslation();
  // Default-open the group containing the active agent so newcomers
  // see the picker isn't empty on first load. Other groups default to
  // collapsed so the sidebar starts compact.
  const groupHasSelection = agents.some((a) => a.id === selectedAgent);
  const { open: persistedOpen, toggle } = useCollapsible(
    `ai.persona_group.${groupId}`,
    groupHasSelection,
  );
  const open = forceOpen || persistedOpen;
  return (
    <div className="space-y-1.5">
      <button
        type="button"
        onClick={toggle}
        aria-expanded={open}
        className="w-full flex items-start gap-1.5 px-1 text-left hover:text-primary transition-colors"
      >
        <span className="mt-0.5 text-muted-foreground shrink-0">
          {open ? (
            <ChevronDown className="h-3 w-3" aria-hidden="true" />
          ) : (
            <ChevronRight className="h-3 w-3" aria-hidden="true" />
          )}
        </span>
        <div className="min-w-0 flex-1">
          <div className="text-[11px] font-semibold text-foreground uppercase tracking-wide flex items-center gap-1.5">
            <span className="truncate">{t(`personas.groups.${groupId}.title`)}</span>
            <span className="text-[10px] text-muted-foreground font-normal normal-case">
              ({agents.length})
            </span>
          </div>
          {open && (
            <div className="text-[10px] text-muted-foreground leading-snug mt-0.5">
              {t(`personas.groups.${groupId}.hint`)}
            </div>
          )}
        </div>
      </button>
      {open && (
        <div className="space-y-1.5">
          {agents.map((a) => (
            <AgentCard
              key={a.id}
              agent={a}
              selected={a.id === selectedAgent}
              onClick={() => onPick(a.id)}
            />
          ))}
        </div>
      )}
    </div>
  );
}

// Reusable list of grouped persona cards — rendered in the desktop
// sidebar AND in the mobile Sheet drawer. Adds (a) a search box that
// filters by agent name + description (i18n-aware), and (b) per-group
// collapse state via `useCollapsible`, so the previously-cramped
// 19-row scroll becomes a scannable accordion. Search hits ignore
// the group collapse so results always render.
function PersonaList({
  agents,
  selectedAgent,
  onPick,
  isLoading,
}: {
  agents: AgentInfo[];
  selectedAgent: string;
  onPick: (id: string) => void;
  isLoading: boolean;
}) {
  const { t, i18n } = useTranslation();
  const [query, setQuery] = useState("");
  const filteredAgents = useMemo(() => {
    const q = query.trim().toLowerCase();
    if (!q) return agents;
    return agents.filter((a) => {
      const nameKey = `personas.agents.${a.id}.name`;
      const descKey = `personas.agents.${a.id}.description`;
      const localName = i18n.exists(nameKey) ? t(nameKey) : a.name;
      const localDesc = i18n.exists(descKey) ? t(descKey) : a.description;
      const haystack = `${a.id} ${localName} ${localDesc}`.toLowerCase();
      return haystack.includes(q);
    });
  }, [agents, query, t, i18n]);

  if (isLoading) {
    return <p className="text-xs text-muted-foreground animate-pulse">{t("ai.loading")}</p>;
  }

  const grouped = new Set(PERSONA_GROUPS.flatMap((g) => g.agentIds));
  const orphans = filteredAgents.filter((a) => !grouped.has(a.id));
  const totalRendered =
    PERSONA_GROUPS.reduce(
      (n, g) => n + filteredAgents.filter((a) => g.agentIds.includes(a.id)).length,
      0,
    ) + orphans.length;
  const hasQuery = query.trim().length > 0;

  return (
    <div className="space-y-3">
      <div className="relative">
        <Search
          className="absolute left-2 top-1/2 -translate-y-1/2 h-3.5 w-3.5 text-muted-foreground pointer-events-none"
          aria-hidden="true"
        />
        <input
          type="search"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          placeholder={t("ai.persona_search_placeholder")}
          aria-label={t("ai.persona_search_placeholder")}
          className="w-full pl-7 pr-2 py-1.5 rounded-md bg-card border border-border text-xs text-foreground focus:outline-none focus:border-primary/50 min-h-[32px]"
        />
      </div>
      {hasQuery && totalRendered === 0 && (
        <p className="text-xs text-muted-foreground px-1">
          {t("ai.persona_search_empty")}
        </p>
      )}
      <div className="space-y-3">
        {PERSONA_GROUPS.map((group) => {
          const groupAgents = group.agentIds
            .map((id) => filteredAgents.find((a) => a.id === id))
            .filter((a): a is AgentInfo => a !== undefined);
          if (groupAgents.length === 0) return null;
          return (
            <PersonaGroupBlock
              key={group.id}
              groupId={group.id}
              agents={groupAgents}
              selectedAgent={selectedAgent}
              onPick={onPick}
              forceOpen={hasQuery}
            />
          );
        })}
        {orphans.length > 0 && (
          <div className="space-y-1.5">
            <div className="px-1 text-[11px] font-semibold text-muted-foreground uppercase tracking-wide">
              {t("ai.other_agents")}
            </div>
            <div className="space-y-1.5">
              {orphans.map((a) => (
                <AgentCard
                  key={a.id}
                  agent={a}
                  selected={a.id === selectedAgent}
                  onClick={() => onPick(a.id)}
                />
              ))}
            </div>
          </div>
        )}
      </div>
    </div>
  );
}

// ── main page ──────────────────────────────────────────────────────

export default function AIPage() {
  const { t, i18n } = useTranslation();
  const token = useAuthStore((s) => s.token);
  const role = useAuthStore((s) => s.user?.role);
  const location = useLocation();
  const navState = location.state as {
    agentId?: string;
    initialMessage?: string;
    context?: Record<string, unknown>;
  } | null;

  const { data: agents = [], isLoading } = useQuery({
    queryKey: ["ai-agents"],
    queryFn: fetchAgents,
  });

  // Quota gauge (today's remaining AI requests). Re-fetches after a
  // successful send via TanStack Query's invalidation in onSuccess
  // below, so the badge counts down in real time.
  const { data: me, refetch: refetchMe } = useQuery({
    queryKey: ["me"],
    queryFn: fetchMe,
    staleTime: 60_000,
  });

  const [selectedAgent, setSelectedAgent] = useState<string>(navState?.agentId ?? "");
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [context] = useState<Record<string, unknown>>(navState?.context ?? {});
  const [input, setInput] = useState(navState?.initialMessage ?? "");
  const [streaming, setStreaming] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [useClaudeAgent, setUseClaudeAgent] = useState(false);
  const [personaSheetOpen, setPersonaSheetOpen] = useState(false);
  const bottomRef = useRef<HTMLDivElement>(null);
  const abortRef = useRef<AbortController | null>(null);
  const sentNavState = useRef(false);

  const canUseClaudeAgent = role === "analyst" || role === "admin";

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect
    if (agents.length && !selectedAgent) setSelectedAgent(agents[0].id);
  }, [agents, selectedAgent]);

  useEffect(() => {
    if (navState?.initialMessage && agents.length && selectedAgent && !sentNavState.current) {
      sentNavState.current = true;
      sendMessage();
    }
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [agents, selectedAgent]);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages]);

  function switchAgent(id: string) {
    if (streaming) return;
    setSelectedAgent(id);
    setMessages([]);
    setError(null);
    setPersonaSheetOpen(false);
  }

  async function sendMessage() {
    const text = input.trim();
    if (!text || streaming || !selectedAgent) return;

    setInput("");
    setError(null);
    const userMsg: ChatMessage = { role: "user", content: text };
    const history = [...messages, userMsg];
    setMessages([...history, { role: "assistant", content: "", streaming: true, toolCalls: [] }]);
    setStreaming(true);

    const activeSpec = agents.find((a) => a.id === selectedAgent);
    const effectiveProvider =
      useClaudeAgent && canUseClaudeAgent && activeSpec?.default_provider !== "claude_agent"
        ? "claude_agent"
        : undefined;

    const ctrl = new AbortController();
    abortRef.current = ctrl;
    let assembled = "";

    const updateLastAssistant = (mutator: (m: ChatMessage) => ChatMessage) => {
      setMessages((prev) => {
        const next = [...prev];
        const last = next[next.length - 1];
        if (last?.role === "assistant") {
          next[next.length - 1] = mutator(last);
        }
        return next;
      });
    };

    try {
      const resp = await fetch("/api/ai/chat", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          Authorization: `Bearer ${token}`,
        },
        body: JSON.stringify({
          agent_id: selectedAgent,
          messages: history.map((m) => ({ role: m.role, content: m.content })),
          context,
          provider: effectiveProvider,
        }),
        signal: ctrl.signal,
      });

      if (!resp.ok) {
        const data = await resp.json().catch(() => ({}));
        if (resp.status === 429) {
          const retryAfter = Number(resp.headers.get("retry-after")) || undefined;
          notifyRateLimited(data.detail, retryAfter);
        }
        throw new Error(data.detail ?? `HTTP ${resp.status}`);
      }

      const reader = resp.body!.getReader();
      const decoder = new TextDecoder();
      let buf = "";

      while (true) {
        const { value, done } = await reader.read();
        if (done) break;
        buf += decoder.decode(value, { stream: true });
        const lines = buf.split("\n\n");
        buf = lines.pop() ?? "";
        for (const line of lines) {
          if (!line.startsWith("data: ")) continue;
          const payload = line.slice(6).trim();
          if (payload === "[DONE]") break;
          try {
            const obj = JSON.parse(payload);
            if (obj.error) { setError(obj.error); break; }
            if (obj.delta) {
              assembled += obj.delta;
              updateLastAssistant((m) => ({ ...m, content: assembled, streaming: true }));
            }
            if (obj.tool_call) {
              const tc: ToolCallEvent = {
                id: obj.tool_call.id,
                name: obj.tool_call.name,
                args: obj.tool_call.args,
                status: "running",
              };
              updateLastAssistant((m) => ({ ...m, toolCalls: [...(m.toolCalls ?? []), tc] }));
            }
            if (obj.tool_result) {
              updateLastAssistant((m) => ({
                ...m,
                toolCalls: (m.toolCalls ?? []).map((tc) =>
                  tc.id === obj.tool_result.id
                    ? { ...tc, result: obj.tool_result.summary,
                        isError: obj.tool_result.is_error,
                        status: obj.tool_result.is_error ? "error" : "done" }
                    : tc
                ),
              }));
            }
          } catch { /* ignore malformed */ }
        }
      }
    } catch (e: unknown) {
      if ((e as Error).name !== "AbortError") {
        setError((e as Error).message);
      }
    } finally {
      setStreaming(false);
      // Finalise: stop spinner on any tool still "running" (e.g. aborted mid-call)
      updateLastAssistant((m) => ({
        ...m,
        streaming: false,
        content: assembled || m.content,
        toolCalls: (m.toolCalls ?? []).map((tc) =>
          tc.status === "running" ? { ...tc, status: "error", result: tc.result ?? "cancelled" } : tc,
        ),
      }));
      // Refresh quota counter once the stream settles.
      void refetchMe();
    }
  }

  function stopGeneration() {
    abortRef.current?.abort();
  }

  function clearChat() {
    if (streaming) return;
    setMessages([]);
    setError(null);
  }

  const activeAgent = agents.find((a) => a.id === selectedAgent);
  const activeAgentName = activeAgent
    ? (i18n.exists(`personas.agents.${activeAgent.id}.name`)
        ? t(`personas.agents.${activeAgent.id}.name`)
        : activeAgent.name)
    : t("ai.header_default");
  const activeAgentDesc = activeAgent
    ? (i18n.exists(`personas.agents.${activeAgent.id}.description`)
        ? t(`personas.agents.${activeAgent.id}.description`)
        : activeAgent.description)
    : "";
  const effectiveIsClaudeAgent =
    activeAgent?.default_provider === "claude_agent" ||
    (useClaudeAgent && canUseClaudeAgent);
  const claudeAgentToggleVisible =
    canUseClaudeAgent && !!activeAgent && activeAgent.default_provider !== "claude_agent";
  const remaining = me?.ai_requests_remaining ?? null;

  return (
    // h-[calc(100vh-2.5rem)] = viewport minus AppLayout's 40px (h-10) topbar.
    // h-full doesn't always resolve reliably through the AppLayout chain
    // (flex-1 main → ErrorBoundary → AIPage), so we pin the height explicitly.
    <div className="h-[calc(100vh-2.5rem)] bg-background flex overflow-hidden">
      {/* Desktop sidebar — always visible on lg+, hidden on mobile (Sheet
          takes over). Keeps the agent list within thumb reach without
          eating mobile screen real estate. */}
      <aside className="hidden lg:flex lg:w-64 border-r border-border flex-col p-4 gap-3 shrink-0 overflow-y-auto">
        <div>
          <h2 className="text-sm font-semibold text-foreground">{t("ai.title")}</h2>
          <p className="text-xs text-muted-foreground mt-0.5">{t("ai.subtitle")}</p>
        </div>
        <PersonaList
          agents={agents}
          selectedAgent={selectedAgent}
          onPick={switchAgent}
          isLoading={isLoading}
        />
        <div className="mt-auto text-xs text-muted-foreground">
          <a href="/dashboard" className="hover:text-foreground transition-colors">{t("ai.back_dashboard")}</a>
        </div>
      </aside>

      {/* Main chat column. */}
      <div className="flex-1 flex flex-col min-w-0 min-h-0">
        <header className="border-b border-border px-3 sm:px-6 py-3 flex items-center gap-2 shrink-0">
          {/* Mobile-only persona picker trigger. */}
          <Sheet open={personaSheetOpen} onOpenChange={setPersonaSheetOpen}>
            <SheetTrigger asChild>
              <button
                type="button"
                className="lg:hidden inline-flex items-center gap-1.5 px-2 py-1 rounded border border-border text-xs text-muted-foreground hover:text-foreground hover:bg-accent/10 min-h-[36px] shrink-0"
                aria-label={t("ai.pick_agent")}
              >
                <Users className="h-4 w-4" aria-hidden="true" />
                <span className="hidden xs:inline">{t("ai.switch")}</span>
              </button>
            </SheetTrigger>
            <SheetContent side="left" className="w-80 max-w-[85vw] overflow-y-auto p-4">
              <SheetHeader className="mb-3">
                <SheetTitle>{t("ai.title")}</SheetTitle>
                <SheetDescription>{t("ai.subtitle")}</SheetDescription>
              </SheetHeader>
              <PersonaList
                agents={agents}
                selectedAgent={selectedAgent}
                onPick={switchAgent}
                isLoading={isLoading}
              />
            </SheetContent>
          </Sheet>

          <div className="min-w-0 flex-1">
            <span className="font-medium text-foreground text-sm truncate block">
              {activeAgentName}
            </span>
            {activeAgentDesc && (
              <span className="hidden sm:block text-xs text-muted-foreground truncate">
                {activeAgentDesc}
              </span>
            )}
          </div>

          <div className="flex items-center gap-2 shrink-0">
            {/* Quota chip — shows today's remaining AI requests so users
                stop burning their daily allowance unawares. Hidden when
                the backend doesn't return a number (admins). */}
            {remaining !== null && (
              <span
                className="text-[11px] tabular-nums px-1.5 py-0.5 rounded border border-border text-muted-foreground"
                title={t("ai.quota_remaining_hint")}
              >
                {t("ai.quota_remaining_short", { remaining })}
              </span>
            )}
            {/* Tools toggle — surfaced inline (was buried in the
                MoreHorizontal dropdown). Analyst+ only, and only when
                the active agent isn't already a hard-wired Claude
                Agent (in which case tools are always on). The chip
                changes color based on state so the user can see at a
                glance whether tools are enabled. */}
            {claudeAgentToggleVisible && (
              <button
                type="button"
                onClick={() => setUseClaudeAgent((v) => !v)}
                disabled={streaming}
                aria-pressed={useClaudeAgent}
                aria-label={t("ai.tools_toggle_aria")}
                title={useClaudeAgent ? t("ai.tools_on_hint") : t("ai.use_tools_hint")}
                className={cn(
                  "hidden sm:inline-flex items-center gap-1 px-2 py-0.5 rounded border text-[11px] transition-colors min-h-[28px] disabled:opacity-50",
                  useClaudeAgent
                    ? "border-amber-700/60 bg-amber-900/30 text-amber-300 hover:bg-amber-900/50"
                    : "border-border text-muted-foreground hover:text-foreground hover:border-primary/40"
                )}
              >
                <Wrench className="h-3 w-3" aria-hidden="true" />
                {useClaudeAgent ? t("ai.tools_on") : t("ai.tools_off")}
              </button>
            )}
            {/* Locked-on indicator — shown when the agent's
                default_provider IS claude_agent so the user can't
                turn it off. Distinct from the toggle above. */}
            {effectiveIsClaudeAgent && !claudeAgentToggleVisible && (
              <span
                className="hidden sm:inline-flex items-center gap-1 text-[11px] text-amber-300"
                title={t("ai.tools_on_hint")}
              >
                <Wrench className="h-3 w-3" aria-hidden="true" />
                {t("ai.tools_on")}
              </span>
            )}
            <DropdownMenu>
              <DropdownMenuTrigger
                aria-label={t("topbar.more")}
                className="p-1.5 rounded hover:bg-accent/10 text-muted-foreground hover:text-foreground transition-colors min-h-[36px] min-w-[36px] flex items-center justify-center"
              >
                <MoreHorizontal className="h-4 w-4" />
              </DropdownMenuTrigger>
              <DropdownMenuContent align="end" className="w-56">
                {/* Mobile-only fallback for the tools toggle (the
                    inline chip is hidden on <sm). Keep the same
                    state machine so desktop and mobile stay in
                    sync regardless of which control was used. */}
                {claudeAgentToggleVisible && (
                  <>
                    <DropdownMenuLabel className="sm:hidden">
                      {t("ai.tools_label")}
                    </DropdownMenuLabel>
                    <DropdownMenuCheckboxItem
                      className="sm:hidden"
                      checked={useClaudeAgent}
                      onCheckedChange={(v) => setUseClaudeAgent(!!v)}
                      disabled={streaming}
                    >
                      {t("ai.use_tools")}
                    </DropdownMenuCheckboxItem>
                    <DropdownMenuSeparator className="sm:hidden" />
                  </>
                )}
                <DropdownMenuItem
                  onSelect={(e) => {
                    if (streaming) {
                      e.preventDefault();
                      return;
                    }
                    clearChat();
                  }}
                  disabled={streaming || messages.length === 0}
                >
                  <Trash2 className="h-3.5 w-3.5" aria-hidden="true" />
                  {t("ai.clear_chat")}
                </DropdownMenuItem>
              </DropdownMenuContent>
            </DropdownMenu>
          </div>
        </header>

        <div className="flex-1 overflow-y-auto px-3 sm:px-6 py-4 space-y-4 min-h-0">
          {messages.length === 0 && (
            <div className="h-full flex items-center justify-center">
              <p className="text-sm text-muted-foreground text-center">
                {activeAgent
                  ? t("ai.ask_placeholder", { agent: activeAgentName })
                  : t("ai.select_to_begin")}
              </p>
            </div>
          )}
          {messages.map((msg, i) => (
            <MessageBubble key={i} msg={msg} />
          ))}
          {error && (
            <div className="text-xs text-red-400 bg-red-950/30 border border-red-900/50 rounded px-3 py-2">
              {error}
            </div>
          )}
          <div ref={bottomRef} />
        </div>

        <ChatComposer
          value={input}
          onChange={setInput}
          onSend={sendMessage}
          onStop={stopGeneration}
          streaming={streaming}
          disabled={!selectedAgent}
          placeholder={t("ai.input_placeholder")}
          disclaimerText={t("ai.disclaimer")}
        />
      </div>
    </div>
  );
}
