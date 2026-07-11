/**
 * Grouped persona picker list — rendered in the AIPage desktop sidebar
 * AND in the mobile Sheet drawer. Extracted verbatim from
 * `pages/AIPage.tsx` (PR-8 巨石頁拆分).
 */
import { useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import { ChevronDown, ChevronRight, Search, Check } from "lucide-react";
import { useCollapsible } from "@/hooks/useCollapsible";
import { cn } from "@/lib/utils";
import type { AgentInfo } from "@/types/discussion";

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
export function PersonaList({
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
