/**
 * AIPage chat header: mobile persona-picker Sheet trigger, active-agent
 * name / description, quota chip, inline Tools ON/OFF toggle and the
 * "more" dropdown (mobile tools checkbox + clear chat). Extracted
 * verbatim from `pages/AIPage.tsx` (PR-8 巨石頁拆分) — all state stays
 * in the page and arrives via props.
 */
import { useTranslation } from "react-i18next";
import { MoreHorizontal, Users, Wrench, Trash2 } from "lucide-react";
import { cn } from "@/lib/utils";
import { PersonaList } from "@/components/ai/PersonaList";
import type { ChatMessage } from "@/components/ai/types";
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

export function ChatHeader({
  personaSheetOpen,
  setPersonaSheetOpen,
  agents,
  selectedAgent,
  switchAgent,
  isLoading,
  activeAgentName,
  activeAgentDesc,
  remaining,
  claudeAgentToggleVisible,
  effectiveIsClaudeAgent,
  useClaudeAgent,
  setUseClaudeAgent,
  streaming,
  messages,
  clearChat,
}: {
  personaSheetOpen: boolean;
  setPersonaSheetOpen: (open: boolean) => void;
  agents: AgentInfo[];
  selectedAgent: string;
  switchAgent: (id: string) => void;
  isLoading: boolean;
  activeAgentName: string;
  activeAgentDesc: string;
  remaining: number | null;
  claudeAgentToggleVisible: boolean;
  effectiveIsClaudeAgent: boolean;
  useClaudeAgent: boolean;
  setUseClaudeAgent: React.Dispatch<React.SetStateAction<boolean>>;
  streaming: boolean;
  messages: ChatMessage[];
  clearChat: () => void;
}) {
  const { t } = useTranslation();
  return (
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
                ? "border-warning/30 bg-warning/10 text-warning hover:bg-warning/20"
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
            className="hidden sm:inline-flex items-center gap-1 text-[11px] text-warning"
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
  );
}
