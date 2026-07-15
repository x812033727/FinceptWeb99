import { useTranslation } from "react-i18next";
import { useQuery } from "@tanstack/react-query";
import { Bot, ClipboardList, Plus, Repeat } from "lucide-react";
import {
  Popover,
  PopoverContent,
  PopoverTrigger,
} from "@/components/ui/popover";
import type { AgentInfo, AutoRunConfig, DiscussionMarket } from "@/types/discussion";
import { AutoRunConfigCard } from "./AutoRunConfigCard";
import { BacktestSweepCard } from "./BacktestSweepCard";
import { StrategyTemplateCard } from "./StrategyTemplateCard";
import { fetchAutoRunConfig } from "./_helpers";

/**
 * Desktop toolbar (PR-E) — replaces the vertical sidebar tool stack
 * with a horizontal row above the transcript. Each tool is a Popover
 * trigger so the card body shows on demand without permanently eating
 * vertical real estate inside the 240px sidebar. Mobile keeps using
 * the sessions Sheet drawer (this component is rendered behind
 * `hidden lg:flex`).
 */
export function DiscussionToolbar({
  agents,
  personaName,
  onNewDiscussion,
  topic,
  rules,
  market,
  personaIds,
}: {
  agents: AgentInfo[];
  personaName: (id: string) => string;
  onNewDiscussion: () => void;
  topic: string;
  rules: string;
  market: DiscussionMarket;
  personaIds: string[];
}) {
  const { t } = useTranslation();

  // Read the auto-run config to surface an ON badge on the trigger
  // when the user has opted in. TanStack Query shares the cache with
  // AutoRunConfigCard so this is a free read.
  const { data: autoRunCfg } = useQuery<AutoRunConfig>({
    queryKey: ["discussion-auto-run-config"],
    queryFn: fetchAutoRunConfig,
  });
  const autoRunOn = !!autoRunCfg?.enabled;

  return (
    <div
      className="hidden lg:flex items-center gap-1.5 border-b border-border bg-card/40 px-4 py-1.5 shrink-0"
      role="toolbar"
      aria-label={t("discussion.toolbar.aria_label", "討論工具列")}
    >
      <button
        type="button"
        onClick={onNewDiscussion}
        className="inline-flex items-center gap-1.5 px-2.5 py-1 rounded-md bg-primary/15 border border-primary/40 text-xs font-medium text-primary hover:bg-primary/25 transition-colors min-h-[28px]"
      >
        <Plus className="h-3.5 w-3.5" aria-hidden="true" />
        {t("discussion.toolbar.new", "新討論")}
      </button>

      <span className="h-5 w-px bg-border/60" aria-hidden="true" />

      <Popover>
        <PopoverTrigger asChild>
          <button
            type="button"
            className="inline-flex items-center gap-1.5 px-2.5 py-1 rounded-md border border-border bg-card text-xs text-foreground hover:border-primary/40 transition-colors min-h-[28px]"
          >
            <Bot className="h-3.5 w-3.5 text-muted-foreground" aria-hidden="true" />
            {t("discussion.toolbar.auto_run", "自動討論")}
            {autoRunOn && (
              <span className="text-micro px-1 py-0 rounded bg-success/10 text-success border border-success/30">
                ON
              </span>
            )}
          </button>
        </PopoverTrigger>
        <PopoverContent
          align="start"
          sideOffset={6}
          className="w-[480px] max-w-[calc(100vw-2rem)] max-h-[calc(100vh-5rem)] overflow-y-auto overscroll-contain p-3"
        >
          <AutoRunConfigCard
            agents={agents}
            collapsed={false}
            onToggleCollapse={() => undefined}
            personaName={personaName}
            hideHeader
          />
        </PopoverContent>
      </Popover>

      <Popover>
        <PopoverTrigger asChild>
          <button
            type="button"
            className="inline-flex items-center gap-1.5 px-2.5 py-1 rounded-md border border-border bg-card text-xs text-foreground hover:border-primary/40 transition-colors min-h-[28px]"
          >
            <ClipboardList className="h-3.5 w-3.5 text-muted-foreground" aria-hidden="true" />
            {t("discussion.toolbar.strategy", "策略模板")}
          </button>
        </PopoverTrigger>
        <PopoverContent
          align="start"
          sideOffset={6}
          className="w-[560px] max-w-[calc(100vw-2rem)] max-h-[70vh] overflow-y-auto p-3"
        >
          <StrategyTemplateCard
            prefill={{
              topic,
              rules,
              market,
              personaIdsCsv: personaIds.join(", "),
            }}
            forceOpen
            hideHeader
          />
        </PopoverContent>
      </Popover>

      <Popover>
        <PopoverTrigger asChild>
          <button
            type="button"
            className="inline-flex items-center gap-1.5 px-2.5 py-1 rounded-md border border-border bg-card text-xs text-foreground hover:border-primary/40 transition-colors min-h-[28px]"
          >
            <Repeat className="h-3.5 w-3.5 text-muted-foreground" aria-hidden="true" />
            {t("discussion.toolbar.sweep", "多日回測")}
          </button>
        </PopoverTrigger>
        <PopoverContent
          align="start"
          sideOffset={6}
          className="w-[640px] max-w-[calc(100vw-2rem)] max-h-[70vh] overflow-y-auto p-3"
        >
          <BacktestSweepCard
            topic={topic}
            rules={rules}
            market={market}
            personaIds={personaIds}
            forceOpen
            hideHeader
          />
        </PopoverContent>
      </Popover>
    </div>
  );
}
