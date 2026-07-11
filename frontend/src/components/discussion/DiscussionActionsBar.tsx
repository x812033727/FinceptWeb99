/**
 * Discussion primary action bar (create / save / run-N-rounds / stop /
 * round progress / conclude / "more" dropdown). Extracted verbatim
 * from `pages/DiscussionPage.tsx` (PR-8 巨石頁拆分) — rendered twice
 * (desktop sticky top bar, mobile bottom bar via `compact`), so all
 * state stays in the page and arrives via props.
 */
import { useTranslation } from "react-i18next";
import { Download, MoreHorizontal, Sparkles, Trash2 } from "lucide-react";
import { cn } from "@/lib/utils";
import type { DiscussionDetail } from "@/types/discussion";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import {
  downloadDiscussionMarkdown,
  rememberRoundsPerClick,
} from "@/components/discussion/_helpers";

export function DiscussionActionsBar({
  compact = false,
  selectedId,
  isDraft,
  isStreaming,
  detail,
  newDiscussion,
  createMut,
  saveEdits,
  updateMut,
  stopStreaming,
  roundsPerClick,
  setRoundsPerClick,
  runRounds,
  loopProgress,
  cancelLoop,
  cancelling,
  concludeMut,
  setInjectSheetOpen,
  runPostMortemFlow,
  postMortemMut,
  personaName,
  deleteMut,
}: {
  compact?: boolean;
  selectedId: string | null;
  isDraft: boolean;
  isStreaming: boolean;
  detail: DiscussionDetail | undefined;
  newDiscussion: () => void;
  createMut: { isPending: boolean };
  saveEdits: () => void;
  updateMut: { isPending: boolean };
  stopStreaming: () => void;
  roundsPerClick: number;
  setRoundsPerClick: (n: number) => void;
  runRounds: () => void;
  loopProgress: { current: number; total: number } | null;
  cancelLoop: () => void;
  cancelling: boolean;
  concludeMut: { isPending: boolean; mutate: () => void };
  setInjectSheetOpen: (open: boolean) => void;
  runPostMortemFlow: () => void;
  postMortemMut: { isPending: boolean };
  personaName: (id: string) => string;
  deleteMut: { isPending: boolean; mutate: (id: string) => void };
}) {
  const { t } = useTranslation();
  const primarySize = compact
    ? "px-3 py-1.5 text-xs"
    : "px-4 py-2 text-sm font-medium";
  return (
    <div className="flex items-center gap-2 flex-wrap">
      {!selectedId ? (
        <button
          onClick={newDiscussion}
          disabled={createMut.isPending}
          className={cn(
            "rounded-md bg-primary text-primary-foreground hover:bg-primary/90 transition-colors disabled:opacity-50 min-h-[36px]",
            primarySize
          )}
        >
          {createMut.isPending ? t("common.saving") : t("discussion.create")}
        </button>
      ) : (
        <>
          {isDraft && (
            <button
              onClick={saveEdits}
              disabled={updateMut.isPending}
              className="px-3 py-1.5 rounded-md border border-border text-xs hover:border-primary/40 transition-colors disabled:opacity-50 min-h-[36px]"
            >
              {updateMut.isPending ? t("common.saving") : t("discussion.save_edits")}
            </button>
          )}
          {isStreaming ? (
            <button
              onClick={stopStreaming}
              className="px-3 py-1.5 rounded-md bg-danger/10 border border-danger/40 text-danger text-xs hover:bg-danger/20 transition-colors min-h-[36px]"
            >
              {t("ai.stop")}
            </button>
          ) : (
            <>
              <input
                type="number"
                min={1}
                max={10}
                value={roundsPerClick}
                onChange={(e) => {
                  const n = Math.min(
                    10,
                    Math.max(1, Number.parseInt(e.target.value, 10) || 1),
                  );
                  setRoundsPerClick(n);
                  rememberRoundsPerClick(n);
                }}
                disabled={loopProgress !== null}
                aria-label={t("discussion.rounds_per_click_label")}
                title={t("discussion.rounds_per_click_label")}
                className="w-14 px-2 py-1 rounded-md border border-border text-xs min-h-[36px] bg-transparent text-foreground"
              />
              <button
                onClick={runRounds}
                className={cn(
                  "rounded-md bg-primary text-primary-foreground hover:bg-primary/90 transition-colors min-h-[36px]",
                  primarySize
                )}
              >
                {t("discussion.run_n_rounds", { n: roundsPerClick })}
              </button>
            </>
          )}
          {loopProgress !== null && (
            <>
              <span className="px-2 py-1 rounded-md border border-border text-xs text-muted-foreground min-h-[36px] inline-flex items-center">
                {t("discussion.round_progress", loopProgress)}
              </span>
              <button
                onClick={cancelLoop}
                disabled={cancelling}
                className="px-3 py-1.5 rounded-md border border-border text-xs text-muted-foreground hover:border-primary/40 transition-colors disabled:opacity-50 min-h-[36px]"
              >
                {cancelling
                  ? t("discussion.cancelling")
                  : t("discussion.cancel_multi_round")}
              </button>
            </>
          )}
          <button
            onClick={() => concludeMut.mutate()}
            disabled={
              isStreaming ||
              concludeMut.isPending ||
              (detail?.current_round ?? 0) === 0
            }
            className="px-3 py-1.5 rounded-md border border-warning/30 text-warning text-xs hover:bg-warning/10 transition-colors disabled:opacity-50 min-h-[36px]"
          >
            {concludeMut.isPending ? t("common.computing") : t("discussion.conclude")}
          </button>
          {/* The "More" dropdown surfaces the lower-frequency
              actions (post-mortem when applicable, inject mid-
              round, delete) so they're always one tap away
              instead of buried below the config form. */}
          <DropdownMenu>
            <DropdownMenuTrigger
              aria-label={t("discussion.more_actions")}
              className="p-1.5 rounded border border-border text-muted-foreground hover:text-foreground hover:border-primary/40 transition-colors min-h-[36px] min-w-[36px] inline-flex items-center justify-center"
            >
              <MoreHorizontal className="h-4 w-4" />
            </DropdownMenuTrigger>
            <DropdownMenuContent align="end" className="w-56">
              <DropdownMenuLabel>{t("discussion.more_actions")}</DropdownMenuLabel>
              {selectedId && isDraft && (detail?.current_round ?? 0) >= 1 && (
                <DropdownMenuItem
                  onSelect={() => setInjectSheetOpen(true)}
                  disabled={isStreaming}
                >
                  <Sparkles className="h-3.5 w-3.5" aria-hidden="true" />
                  {t("discussion.inject_send")}
                </DropdownMenuItem>
              )}
              {detail?.as_of_date && detail?.conclusion && (
                <DropdownMenuItem
                  onSelect={runPostMortemFlow}
                  disabled={
                    isStreaming ||
                    postMortemMut.isPending ||
                    concludeMut.isPending
                  }
                >
                  <Sparkles className="h-3.5 w-3.5 text-purple-300" aria-hidden="true" />
                  {postMortemMut.isPending
                    ? t("discussion.post_mortem_running")
                    : t("discussion.post_mortem")}
                </DropdownMenuItem>
              )}
              {detail && (
                <DropdownMenuItem
                  onSelect={() => downloadDiscussionMarkdown(detail, personaName)}
                >
                  <Download className="h-3.5 w-3.5" aria-hidden="true" />
                  {t("discussion.export_markdown")}
                </DropdownMenuItem>
              )}
              <DropdownMenuSeparator />
              <DropdownMenuItem
                onSelect={() => deleteMut.mutate(selectedId!)}
                disabled={deleteMut.isPending || isStreaming}
                className="text-danger focus:text-danger/80"
              >
                <Trash2 className="h-3.5 w-3.5" aria-hidden="true" />
                {t("common.delete")}
              </DropdownMenuItem>
            </DropdownMenuContent>
          </DropdownMenu>
        </>
      )}
    </div>
  );
}
