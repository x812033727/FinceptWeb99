/**
 * DiscussionPage server mutations (create / update / delete / conclude
 * / post-mortem / inject) + the post-mortem orchestration wrapper.
 * Extracted verbatim from `pages/DiscussionPage.tsx` (PR-8 巨石頁拆分)
 * — only the closure variables became hook parameters; the bodies are
 * unchanged so behavior is identical.
 */
import { useMemo, useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { errorDetail } from "@/lib/api";
import type { DiscussionMarket } from "@/types/discussion";
import {
  concludeSession,
  createSession,
  deleteSession,
  injectUserMessage,
  interjectSession,
  runPostMortem,
  runPostMortemFlowSteps,
  readPostMortemResult,
  rememberPostMortemResult,
  rememberRules,
  rememberTopic,
  updateSession,
} from "@/components/discussion/_helpers";
import type { PostMortemResponse } from "@/components/discussion/_helpers";

export function useDiscussionMutations({
  selectedId,
  setSelectedId,
  isStreaming,
  setStreamError,
  runOneRound,
}: {
  selectedId: string | null;
  setSelectedId: (id: string | null) => void;
  isStreaming: boolean;
  setStreamError: (msg: string | null) => void;
  runOneRound: () => Promise<{ ok: boolean; rateLimited: boolean }>;
}) {
  const queryClient = useQueryClient();

  const createMut = useMutation({
    mutationFn: createSession,
    onSuccess: (row) => {
      queryClient.invalidateQueries({ queryKey: ["discussion-sessions"] });
      setSelectedId(row.id);
      // Successful create implies the user is happy with these topic /
      // rules — make them the defaults for the next "+ New Discussion".
      rememberTopic(row.topic);
      rememberRules(row.rules);
    },
    // Without this, a backend failure (validation 400, 422, server 500,
    // network) leaves the user staring at a button that re-enables but
    // produces no visible feedback — perfectly indistinguishable from
    // "the click was ignored". Surface the detail through the same
    // streamError banner that round-failures use.
    onError: (err) => setStreamError(errorDetail(err)),
  });

  const updateMut = useMutation({
    mutationFn: (body: {
      topic?: string;
      rules?: string;
      persona_ids?: string[];
      market?: DiscussionMarket;
    }) => updateSession(selectedId!, body),
    onSuccess: (row) => {
      queryClient.invalidateQueries({ queryKey: ["discussion-sessions"] });
      queryClient.invalidateQueries({ queryKey: ["discussion-session", selectedId] });
      // Whatever the user just persisted becomes the default for the
      // next new discussion.
      rememberTopic(row.topic);
      rememberRules(row.rules);
    },
    onError: (err) => setStreamError(errorDetail(err)),
  });

  const deleteMut = useMutation({
    mutationFn: deleteSession,
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["discussion-sessions"] });
      setSelectedId(null);
    },
    onError: (err) => setStreamError(errorDetail(err)),
  });

  const concludeMut = useMutation({
    mutationFn: () => concludeSession(selectedId!),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["discussion-session", selectedId] });
      // Bust any cached scoreboard response — including 400 errors
      // from earlier attempts when the discussion had no conclusion
      // yet. Without this, the ScoreboardCard would keep showing the
      // pre-conclusion error state until the staleTime (60s) expires
      // or the user manually re-opens the card. Backtest discussions
      // are the worst-hit: their D1-D5 data is fully available the
      // moment the conclusion lands, but the user can't see it.
      queryClient.invalidateQueries({ queryKey: ["discussion-scoreboard", selectedId] });
    },
    onError: (err) => setStreamError(errorDetail(err)),
  });

  // Post-mortem self-critique flow (backtest mode only). Injects the
  // top-N next-day gainers as a `user_input` turn so the next round's
  // personas have to defend / revise their conclusion against ground
  // truth. This mutation only handles the inject step; the caller
  // (`runPostMortemFlow` below) chains it with a new round + a re-
  // conclude so the user clicks ONE button.
  const postMortemMut = useMutation({
    mutationFn: () => runPostMortem(selectedId!),
    onSuccess: (data) => {
      // Refetch session to pull in the injected turn so the persona
      // history sent to the next round actually contains the critique
      // prompt.
      queryClient.invalidateQueries({ queryKey: ["discussion-session", selectedId] });
      // PR #268: persist the gainers payload so the leaderboard card
      // survives a page reload — the mutation result is otherwise
      // in-memory only and operators kept reloading and wondering
      // "did the post-mortem run".
      if (selectedId && data) {
        rememberPostMortemResult(selectedId, data);
      }
    },
  });

  // Hydrate the persisted post-mortem result on session switch so
  // the leaderboard card re-appears after a reload. The mutation
  // state still wins when fresh — `postMortemMut.data` shadows this
  // for the rest of the active session. Computed during render with
  // useMemo since the value is purely a function of `selectedId`
  // (avoids the react-hooks/set-state-in-effect lint).
  const persistedPostMortem = useMemo<PostMortemResponse | null>(
    () => (selectedId ? readPostMortemResult(selectedId) : null),
    [selectedId],
  );

  /** Three-step chain triggered by 「事後檢討」: inject → run round →
   *  re-conclude. Each step persists independently so a failure at
   *  step 2 or 3 leaves a recoverable state (the user can manually
   *  re-run from the partial state).
   */
  async function runPostMortemFlow() {
    // Orchestration extracted into `runPostMortemFlowSteps`
    // (PR #279) for unit-testability — this thin wrapper just
    // wires the page's stateful dependencies to the pure helper.
    await runPostMortemFlowSteps({
      canStart: () =>
        Boolean(selectedId) && !isStreaming && !postMortemMut.isPending,
      runPostMortem: () => postMortemMut.mutateAsync(),
      runRound: async () => { await runOneRound(); },
      runConclude: () => {
        if (selectedId) concludeMut.mutate();
      },
      onError: (detail) => setStreamError(detail),
      onSkipped: (verdict) => {
        // Win-skip: surface a toast-style banner. The PostMortemSkipped
        // card (rendered alongside ConclusionCard) carries the full
        // verdict detail; this is just acknowledging the click.
        const best = verdict?.best_pct;
        const msg = best != null
          ? `✅ 推薦已達標 (peak ${best >= 0 ? "+" : ""}${best.toFixed(2)}%) — 跳過事後檢討`
          : "✅ 推薦已達標 — 跳過事後檢討";
        setStreamError(msg);
      },
    });
  }

  // Between-rounds user injection (PR #211). Drops a user_input
  // turn into the current round's transcript so the next round's
  // personas have to react to it.
  const [injectDraft, setInjectDraft] = useState("");
  const injectMut = useMutation({
    mutationFn: (content: string) => injectUserMessage(selectedId!, content),
    onSuccess: () => {
      setInjectDraft("");
      queryClient.invalidateQueries({ queryKey: ["discussion-session", selectedId] });
    },
  });

  // B4: mid-round interjection / post-conclusion 追問. While a round
  // streams, the backend queues the question and answers it at the
  // next turn boundary (the turns then arrive over the round's SSE
  // stream); on a concluded discussion the single follow-up turn runs
  // synchronously and we refetch the session to surface it.
  const [interjectTarget, setInterjectTarget] = useState("");
  const interjectMut = useMutation({
    mutationFn: (args: { question: string; target_persona?: string }) =>
      interjectSession(selectedId!, args),
    onSuccess: (data) => {
      setInjectDraft("");
      if (data.status === "answered") {
        queryClient.invalidateQueries({
          queryKey: ["discussion-session", selectedId],
        });
      }
    },
    onError: (err) => setStreamError(errorDetail(err)),
  });

  return {
    createMut,
    updateMut,
    deleteMut,
    concludeMut,
    postMortemMut,
    persistedPostMortem,
    runPostMortemFlow,
    injectDraft,
    setInjectDraft,
    injectMut,
    interjectTarget,
    setInterjectTarget,
    interjectMut,
  };
}
