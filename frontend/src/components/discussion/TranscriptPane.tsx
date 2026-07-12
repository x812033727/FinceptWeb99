/**
 * Discussion transcript pane: per-round sections, the live streaming /
 * preparing cards, conclusion + post-mortem cards, round contexts and
 * the scoreboard. Extracted verbatim from `pages/DiscussionPage.tsx`
 * (PR-8 巨石頁拆分) — the streaming state lives in `_useRoundStream`
 * (page-owned) and arrives here via props.
 */
import { useTranslation } from "react-i18next";
import type {
  DiscussionDetail,
  PersonaUsageDetail,
  Turn,
} from "@/types/discussion";
import { ConclusionCard } from "@/components/discussion/ConclusionCard";
import { ConclusionDiffCard } from "@/components/discussion/ConclusionDiffCard";
import { InjectForm } from "@/components/discussion/InjectForm";
import type { InjectFormProps, InjectMode } from "@/components/discussion/InjectForm";
import { PostMortemSkippedCard } from "@/components/discussion/PostMortemSkippedCard";
import { PostMortemGainersCard } from "@/components/discussion/PostMortemGainersCard";
import { RoundContextsCard } from "@/components/discussion/RoundContextsCard";
import { RoundDivider } from "@/components/discussion/RoundDivider";
import { RoundSection } from "@/components/discussion/RoundSection";
import { ScoreboardCard } from "@/components/discussion/ScoreboardCard";
import { StreamingTurnCard } from "@/components/discussion/StreamingTurnCard";
import { formatDateLong } from "@/components/discussion/_helpers";
import type { PostMortemResponse, RoundUsage } from "@/components/discussion/_helpers";

export function TranscriptPane({
  selectedId,
  detail,
  transcript,
  personaName,
  personaShort,
  isStreaming,
  streamingPersona,
  streamingRound,
  streamingStage,
  streamingProgress,
  streamBuffer,
  streamingToolEvents,
  persistedRoundUsage,
  persistedRoundUsageDetail,
  liveRoundUsage,
  liveRoundUsageDetail,
  postMortemMut,
  persistedPostMortem,
  bottomRef,
  injectMode,
  injectFormProps,
}: {
  selectedId: string | null;
  detail: DiscussionDetail | undefined;
  transcript: Turn[];
  personaName: (id: string) => string;
  personaShort: (id: string) => string | undefined;
  isStreaming: boolean;
  streamingPersona: string | null;
  streamingRound: number | null;
  streamingStage: string | null;
  streamingProgress: { done: number; total: number } | null;
  streamBuffer: string;
  streamingToolEvents: Array<{
    id: string;
    kind: "call" | "result";
    name: string;
    args?: unknown;
    summary?: string;
    is_error?: boolean;
  }>;
  persistedRoundUsage: RoundUsage[];
  persistedRoundUsageDetail: PersonaUsageDetail[];
  liveRoundUsage: Record<number, { prompt: number; completion: number; total: number }>;
  liveRoundUsageDetail: Record<number, PersonaUsageDetail[]>;
  postMortemMut: { data: PostMortemResponse | undefined };
  persistedPostMortem: PostMortemResponse | null;
  bottomRef: React.RefObject<HTMLDivElement>;
  /** B4: current interjection mode — "running" renders the interject
   *  form under the live streaming card, "followup" renders the 追問
   *  form under the conclusion. Omitted / "between" renders nothing
   *  here (the between-rounds form lives in the config bar). */
  injectMode?: InjectMode;
  injectFormProps?: Omit<InjectFormProps, "mode">;
}) {
  const { t } = useTranslation();

  // Latest round number — used to default-expand the most recent
  // RoundSection so newcomers see content instead of a wall of
  // collapsed headers.
  const latestRound = transcript.length
    ? Math.max(...transcript.map((tn) => tn.round))
    : 0;

  return (
    <div className="flex-1 overflow-y-auto px-4 py-4 space-y-3 min-h-0">
      {!selectedId && (
        <div className="h-full flex items-center justify-center">
          <p className="text-sm text-muted-foreground">{t("discussion.intro")}</p>
        </div>
      )}
      {selectedId && detail && (
        <div className="text-[11px] text-muted-foreground border-b border-border pb-2 flex flex-wrap items-center gap-x-3 gap-y-1">
          {/* PR #277: backtest discussions surface the as_of
              date prominently — that's the day being analysed,
              far more useful than when the row was created. */}
          {detail.as_of_date && (
            <span className="px-1.5 py-0.5 rounded bg-blue-900/20 text-blue-300 border border-blue-800/40 text-[10px] uppercase tracking-wider">
              {t("discussion.as_of_date")}：{detail.as_of_date}
            </span>
          )}
          <span>
            {t("discussion.created_at")}：{formatDateLong(detail.created_at)}
          </span>
          {detail.updated_at && detail.updated_at !== detail.created_at && (
            <span>
              {t("discussion.updated_at")}：{formatDateLong(detail.updated_at)}
            </span>
          )}
        </div>
      )}
      {selectedId && transcript.length === 0 && !isStreaming && (
        <div className="h-full flex items-center justify-center">
          <p className="text-sm text-muted-foreground">{t("discussion.click_to_start")}</p>
        </div>
      )}
      {/* Group transcript by round so each can collapse independently.
          Default-collapsed per UX requirement — users want a clean
          overview screen, not a wall of multi-round text. The live
          streaming card below stays outside these groups so in-flight
          content is always visible during a round. */}
      {(() => {
        if (!selectedId || transcript.length === 0) return null;
        const byRound = new Map<number, Turn[]>();
        for (const tn of transcript) {
          const list = byRound.get(tn.round) ?? [];
          list.push(tn);
          byRound.set(tn.round, list);
        }
        const orderedRounds = Array.from(byRound.keys()).sort(
          (a, b) => a - b,
        );
        // Merge persisted per-round token tallies (reopened
        // discussions) with live round_end events (current run).
        // Live wins — it's the freshest settlement for that round.
        const usageMap: Record<
          number,
          { prompt: number; completion: number; total: number }
        > = {};
        for (const u of persistedRoundUsage) {
          usageMap[u.round] = {
            prompt: u.prompt_tokens,
            completion: u.completion_tokens,
            total: u.total_tokens,
          };
        }
        Object.assign(usageMap, liveRoundUsage);
        // Per-round usage detail (per-persona + breakdown). Group
        // persisted rows by round, then let live turn_end rows win
        // for the in-progress round.
        const detailMap: Record<number, PersonaUsageDetail[]> = {};
        for (const d of persistedRoundUsageDetail) {
          (detailMap[d.round] ??= []).push(d);
        }
        Object.assign(detailMap, liveRoundUsageDetail);
        return orderedRounds.map((rn) => (
          <RoundSection
            key={rn}
            discussionId={selectedId}
            round={rn}
            turns={byRound.get(rn) ?? []}
            personaName={personaName}
            personaInitial={personaShort}
            defaultExpanded={rn === latestRound}
            tokens={usageMap[rn]}
            usageDetail={detailMap[rn]}
          />
        ));
      })()}
      {/* Pre-turn-stream "round preparing" card. Backend emits
          `round_start` immediately after bumping the discussion's
          current_round, but `gather_market_context` (incl. inline
          news-sentiment scoring on backtest archives, ~15-30s)
          runs BEFORE the first `turn_start` event. Without this
          card the UI shows nothing during that window — users
          think round 2 didn't fire. Render whenever we're
          streaming but haven't yet received the first persona's
          turn_start.
      */}
      {isStreaming && !streamingPersona && (
        <>
          {streamingRound !== null && (
            <div className="flex items-center gap-2 my-3">
              <span className="text-[11px] font-semibold text-primary tracking-wider">
                {t("discussion.round_label", { round: streamingRound })}
              </span>
              <span className="flex-1 h-px bg-border" />
            </div>
          )}
          <div className="bg-card border border-primary/40 rounded-lg p-3">
            <div className="flex items-center gap-2 text-xs text-muted-foreground">
              <span
                aria-hidden="true"
                className="inline-block w-2 h-2 rounded-full bg-primary animate-pulse"
              />
              <span>
                {/* Backend ctx_progress event (PR #252) drives
                    a more specific stage descriptor when one is
                    available; falls back to the generic loading
                    copy otherwise (older backend / pre-progress-
                    event window). i18n key
                    `discussion.loading_stage_<stage>` so adding a
                    new backend stage just needs a new i18n entry
                    without code changes. Untranslated stages
                    fall back to `loading_context` so the user
                    never sees a raw key. */}
                {(() => {
                  let base: string;
                  if (streamingStage) {
                    const i18nKey = `discussion.loading_stage_${streamingStage}`;
                    const translated = t(i18nKey);
                    base = translated !== i18nKey
                      ? translated
                      : (streamingRound === null
                          ? t("discussion.loading_context_initial")
                          : t("discussion.loading_context"));
                  } else {
                    base = streamingRound === null
                      ? t("discussion.loading_context_initial")
                      : t("discussion.loading_context");
                  }
                  // C1-3: append `(done/total)` when the backend
                  // ships a sub-counter — currently the per-symbol
                  // news fan-out. Older backends omit the fields
                  // so the suffix never appears.
                  if (streamingProgress) {
                    return `${base} (${streamingProgress.done}/${streamingProgress.total})`;
                  }
                  return base;
                })()}
              </span>
            </div>
          </div>
        </>
      )}
      {isStreaming && streamingPersona && (
        <>
          {/* When the streaming round hasn't appeared in the transcript
              yet (first persona of a fresh round), surface a
              RoundDivider so the user always knows which round is
              being generated. */}
          {streamingRound !== null &&
            (transcript.length === 0 ||
              transcript[transcript.length - 1].round !== streamingRound) && (
              <RoundDivider round={streamingRound} />
            )}
          <StreamingTurnCard
            personaId={streamingPersona}
            personaName={personaName(streamingPersona)}
            personaInitial={personaShort(streamingPersona)}
            round={streamingRound}
            streamBuffer={streamBuffer}
            toolEvents={streamingToolEvents}
          />
        </>
      )}
      {/* B4: mid-round interject form — visible while the round
          streams so the owner can raise a question without opening
          the config drawer. The backend answers it at the next turn
          boundary; the resulting turns arrive over the same SSE
          stream and render above with 使用者提問 / 插話回覆 badges. */}
      {isStreaming && injectMode === "running" && injectFormProps && (
        <InjectForm mode="running" {...injectFormProps} />
      )}

      {detail?.conclusion && (
        <ConclusionCard detail={detail} personaName={personaName} />
      )}
      {/* PR-2: structured diff between original and revised
          conclusions — sits between the two cards so the
          operator sees "what changed" without comparing by eye.
          Hides itself when `post_mortem_diff` is null (no
          post-mortem, parse-errored side, or pre-PR-2 row). */}
      <ConclusionDiffCard diff={detail?.post_mortem_diff} />
      {/* PR #272: post-mortem self-critique conclusion lands in
          its own purple card alongside the original. Backend
          writes here only when `has_post_mortem` is detected
          in the transcript, so this conditional matches the
          backend's routing logic. */}
      {detail?.post_mortem_conclusion && (
        <ConclusionCard
          detail={detail}
          personaName={personaName}
          conclusion={detail.post_mortem_conclusion}
          variant="post_mortem"
        />
      )}
      {/* B4: post-conclusion 追問 affordance — one bounded follow-up
          question answered by a single persona, appended to the
          transcript without re-opening full rounds. Sits right under
          the conclusion cards where the question naturally arises. */}
      {injectMode === "followup" && injectFormProps && detail?.conclusion && (
        <InjectForm mode="followup" {...injectFormProps} />
      )}
      {/* Post-mortem gainers leaderboard. Falls back to the
          localStorage-persisted snapshot (PR #268) when the
          mutation state is empty — survives page reload so the
          operator can see "what was the post-mortem about" after
          coming back to the discussion. */}
      <PostMortemGainersCard
        data={postMortemMut.data ?? persistedPostMortem}
      />
      {/* Win-skip card (learning-loop PR). Renders only when the
          backend returned status="skipped" — i.e. the recommendation
          already cleared the threshold so no critique round was
          fired. Pulls from both the in-memory mutation result AND
          the persisted snapshot so it survives a page reload. */}
      <PostMortemSkippedCard
        data={postMortemMut.data ?? persistedPostMortem}
      />
      {detail && (
        <RoundContextsCard discussionId={detail.id} />
      )}
      {detail && (
        <ScoreboardCard
          discussionId={detail.id}
          hasConclusion={Boolean(detail.conclusion)}
        />
      )}
      <div ref={bottomRef} />
    </div>
  );
}
