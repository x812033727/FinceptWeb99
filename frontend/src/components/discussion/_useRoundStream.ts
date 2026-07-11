/**
 * Round-streaming engine for DiscussionPage: all per-round SSE state
 * (streaming turns / persona / buffer / stage / tool events / usage
 * tallies), the `runOneRound` SSE loop, the multi-round driver
 * `runRounds` and the cancel/stop controls. Extracted verbatim from
 * `pages/DiscussionPage.tsx` (PR-8 巨石頁拆分) — only the closure
 * variables became hook parameters; the bodies are unchanged so
 * behavior is identical.
 */
import { useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { useQueryClient } from "@tanstack/react-query";
import api, { notifyRateLimited } from "@/lib/api";
import { useAuthStore } from "@/store/authStore";
import { useToastStore } from "@/store/toastStore";
import type {
  Discussion,
  DiscussionDetail,
  PersonaUsageDetail,
  Turn,
} from "@/types/discussion";
import { readRoundsPerClick } from "@/components/discussion/_helpers";

export function useRoundStream({
  selectedId,
  personaIds,
}: {
  selectedId: string | null;
  personaIds: string[];
}) {
  const { t } = useTranslation();
  const token = useAuthStore((s) => s.token);
  const queryClient = useQueryClient();
  const pushToast = useToastStore((s) => s.push);

  // Streaming round state — appended to as the SSE arrives.
  const [streamingTurns, setStreamingTurns] = useState<Turn[]>([]);
  const [streamingPersona, setStreamingPersona] = useState<string | null>(null);
  const [streamBuffer, setStreamBuffer] = useState("");
  const [streamingRound, setStreamingRound] = useState<number | null>(null);
  // Backend's `ctx_progress` SSE event — fires at ctx-gathering
  // milestones (`fetching_market_data` / `scoring_news_sentiment` /
  // `ctx_ready`) so the preparing card (PR #244) can show what's
  // happening during the silent 15-30 s window. Null when no
  // progress event has arrived yet (early startup).
  const [streamingStage, setStreamingStage] = useState<string | null>(null);
  // C1-3: when the per-symbol news fan-out reports a counter,
  // surface it as "Scoring news sentiment 3/5" in the preparing
  // card. Null when the current stage doesn't ship done/total
  // (every other phase, or a backend that pre-dates C1-3).
  const [streamingProgress, setStreamingProgress] = useState<
    { done: number; total: number } | null
  >(null);
  const [isStreaming, setIsStreaming] = useState(false);
  const [streamError, setStreamError] = useState<string | null>(null);
  // Per-round token tally (input + output) fed to the AI. Populated live
  // from `round_end` SSE events; the historical query below seeds it for
  // reopened discussions. Keyed by round number.
  const [liveRoundUsage, setLiveRoundUsage] = useState<
    Record<number, { prompt: number; completion: number; total: number }>
  >({});
  // Per-round, per-persona usage + prompt-composition breakdown for the
  // "ctx 用量明細" panel. Populated live from `turn_end` (tokens +
  // breakdown; cost arrives via the persisted query refetch). Keyed by
  // round number → list of persona rows.
  const [liveRoundUsageDetail, setLiveRoundUsageDetail] = useState<
    Record<number, PersonaUsageDetail[]>
  >({});
  // Live tool-use log for the persona currently streaming. Cleared on
  // every turn_start so each persona's bubble shows only its own
  // tool calls. Not persisted — once turn_end fires we drop them; the
  // persona's `content` is meant to summarise what they learned from
  // the tools, so the bubble's final text already encodes the
  // signal. Server-side these events are emitted for `claude_agent`
  // and OpenAI-compat tool-loop providers (PR #208).
  const [streamingToolEvents, setStreamingToolEvents] = useState<
    Array<{
      id: string;
      kind: "call" | "result";
      name: string;
      args?: unknown;
      summary?: string;
      is_error?: boolean;
    }>
  >([]);
  const abortRef = useRef<AbortController | null>(null);
  // Multi-round driver: lets the user fire N consecutive rounds with
  // one click. `loopProgress` drives the "Round X of N" badge + the
  // graceful-cancel button; `cancelRequestedRef` is a plain ref because
  // only the loop body reads it between iterations and a state flip
  // would re-render without effect.
  const [roundsPerClick, setRoundsPerClick] = useState<number>(readRoundsPerClick);
  const [loopProgress, setLoopProgress] = useState<{ current: number; total: number } | null>(null);
  // Two-headed cancel signal: the ref is what the loop body checks
  // between iterations (synchronous, no stale-closure pitfalls), the
  // state is what flips the cancel button's label to "Cancelling…" so
  // the user gets immediate visual feedback.
  const cancelRequestedRef = useRef(false);
  const [cancelling, setCancelling] = useState(false);

  // Reset all per-session streaming state when the user switches to a
  // different discussion. Keyed on `selectedId` (not `detail`) so the
  // optimistic cache mutations during a round don't nuke the streaming
  // overlay mid-stream.
  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect
    setStreamingTurns([]);
    setStreamBuffer("");
    setStreamingPersona(null);
    setStreamingRound(null);
    setStreamingStage(null);
    setStreamingProgress(null);
    setStreamError(null);
    setStreamingToolEvents([]);
    setLiveRoundUsage({});
    setLiveRoundUsageDetail({});
  }, [selectedId]);

  async function runOneRound(): Promise<{ ok: boolean; rateLimited: boolean }> {
    if (!selectedId) return { ok: false, rateLimited: false };
    setIsStreaming(true);
    setStreamError(null);
    setStreamingTurns([]);
    setStreamBuffer("");
    setStreamingPersona(null);
    setStreamingRound(null);
    setStreamingStage(null);
    setStreamingProgress(null);

    const ctrl = new AbortController();
    abortRef.current = ctrl;
    let roundOk = true;
    let rateLimited = false;

    try {
      const resp = await fetch(`/api/discussion/sessions/${selectedId}/round`, {
        method: "POST",
        headers: { Authorization: `Bearer ${token}` },
        signal: ctrl.signal,
      });

      if (!resp.ok) {
        const data = await resp.json().catch(() => ({}));
        if (resp.status === 429) {
          const retryAfter = Number(resp.headers.get("retry-after")) || undefined;
          notifyRateLimited(data.detail, retryAfter);
          // Quota exhausted is a hard, round-fatal condition — flag it so
          // the multi-round driver stops cleanly instead of hammering the
          // remaining rounds with requests that will all 429.
          rateLimited = true;
        }
        throw new Error(data.detail ?? `HTTP ${resp.status}`);
      }

      const reader = resp.body!.getReader();
      const decoder = new TextDecoder();
      let buf = "";

      let currentBuffer = "";

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
            switch (obj.type) {
              case "round_start":
                setStreamingRound(obj.round);
                // Optimistically advance the cached `current_round` so the
                // sidebar R-counter, the "Next round" button label, and the
                // conclude button's enabled state all reflect the new round
                // immediately — without waiting for the post-stream refetch
                // (which can race / get blocked by a stale-window cache /
                // fail silently on a slow network and leave the UI stuck
                // at R1 with conclude disabled).
                if (typeof obj.round === "number") {
                  queryClient.setQueryData<DiscussionDetail | undefined>(
                    ["discussion-session", selectedId],
                    (old) => (old ? { ...old, current_round: obj.round } : old),
                  );
                  queryClient.setQueryData<Discussion[] | undefined>(
                    ["discussion-sessions"],
                    (old) =>
                      old
                        ? old.map((s) =>
                            s.id === selectedId
                              ? { ...s, current_round: obj.round }
                              : s,
                          )
                        : old,
                  );
                }
                break;
              case "ctx_progress":
                // Backend ctx-gathering milestone — drives the
                // preparing card's stage descriptor (PR #244 +
                // #252). Stages today: `fetching_market_data` /
                // `scoring_news_sentiment` / `ctx_ready`. The card
                // looks up an i18n key per stage; unknown stages
                // fall back to the generic loading copy so a
                // backend that adds a new stage doesn't break
                // older frontends.
                if (typeof obj.stage === "string") {
                  setStreamingStage(obj.stage);
                }
                // C1-3: optional `done` / `total` sub-counter from
                // the per-symbol news fan-out. Both present →
                // render `(X/Y)`; either missing → clear so a
                // later stage without a counter doesn't keep a
                // stale fraction on screen.
                if (
                  typeof obj.done === "number" &&
                  typeof obj.total === "number"
                ) {
                  setStreamingProgress({ done: obj.done, total: obj.total });
                } else {
                  setStreamingProgress(null);
                }
                break;
              case "turn_start":
                currentBuffer = "";
                setStreamingPersona(obj.persona_id);
                if (typeof obj.round === "number") setStreamingRound(obj.round);
                setStreamBuffer("");
                setStreamingToolEvents([]);
                break;
              case "delta":
                currentBuffer += obj.text;
                setStreamBuffer(currentBuffer);
                break;
              case "tool_call":
                // Live tool-use feedback: append a `call` row keyed
                // on the LLM's tool-call id so the matching
                // `tool_result` later can mark it complete instead
                // of producing two rows. `id` may be missing on
                // some providers — fall back to a random key.
                setStreamingToolEvents((prev) => [
                  ...prev,
                  {
                    id:    String(obj.id ?? `${Date.now()}-${prev.length}`),
                    kind:  "call",
                    name:  String(obj.name ?? "unknown"),
                    args:  obj.args,
                  },
                ]);
                break;
              case "tool_result":
                setStreamingToolEvents((prev) => {
                  // If we already have a `call` row with this id,
                  // upgrade it to a `result` row carrying the
                  // summary; otherwise append a standalone result
                  // (some providers fire only the result event).
                  const callIdx = prev.findIndex(
                    (e) => e.id === String(obj.id) && e.kind === "call",
                  );
                  const resultRow = {
                    id:       String(obj.id ?? `${Date.now()}-${prev.length}`),
                    kind:     "result" as const,
                    name:     String(obj.name ?? "unknown"),
                    summary:  String(obj.summary ?? ""),
                    is_error: Boolean(obj.is_error),
                  };
                  if (callIdx < 0) return [...prev, resultRow];
                  const next = [...prev];
                  next[callIdx] = { ...next[callIdx], ...resultRow };
                  return next;
                });
                break;
              case "turn_end":
                setStreamingTurns((prev) => [
                  ...prev,
                  {
                    id: Date.now() + prev.length,
                    round: obj.round,
                    turn_index: obj.turn_index,
                    persona_id: obj.persona_id,
                    stance: obj.stance,
                    content: obj.content,
                    created_at: new Date().toISOString(),
                  },
                ]);
                // Live per-persona usage + prompt-composition breakdown
                // for the "ctx 用量明細" panel. Cost isn't computed
                // client-side; the persisted query refetch back-fills it
                // after the round completes.
                if (typeof obj.round === "number") {
                  const rn = obj.round as number;
                  const pd: PersonaUsageDetail = {
                    round: rn,
                    persona_id: String(obj.persona_id),
                    prompt_tokens: Number(obj.prompt_tokens ?? 0),
                    completion_tokens: Number(obj.completion_tokens ?? 0),
                    total_tokens:
                      Number(obj.prompt_tokens ?? 0) +
                      Number(obj.completion_tokens ?? 0),
                    cost_usd: 0,
                    tool_call_count: Number(obj.tool_call_count ?? 0),
                    breakdown:
                      (obj.breakdown as PersonaUsageDetail["breakdown"]) ?? null,
                  };
                  setLiveRoundUsageDetail((prev) => {
                    const list = (prev[rn] ?? []).filter(
                      (d) => d.persona_id !== pd.persona_id,
                    );
                    return { ...prev, [rn]: [...list, pd] };
                  });
                }
                setStreamBuffer("");
                setStreamingPersona(null);
                setStreamingToolEvents([]);
                currentBuffer = "";
                break;
              case "round_end":
                // Per-round token settlement (input + output) for the
                // "每輪結算給 AI 的 token" display. Backend sums the
                // round's llm_usage_events before emitting this.
                if (typeof obj.round === "number") {
                  const rn = obj.round as number;
                  setLiveRoundUsage((prev) => ({
                    ...prev,
                    [rn]: {
                      prompt: Number(obj.prompt_tokens ?? 0),
                      completion: Number(obj.completion_tokens ?? 0),
                      total: Number(obj.total_tokens ?? 0),
                    },
                  }));
                }
                break;
              case "error":
                setStreamError(obj.message ?? "未知錯誤");
                // Per-persona soft errors (timeout / LLM error) carry a
                // persona_id and the round still completes — they must NOT
                // abort the multi-round loop. Only round-fatal errors (no
                // persona_id, emitted by the router) do.
                if (!obj.persona_id) roundOk = false;
                break;
            }
          } catch {
            /* malformed event — skip */
          }
        }
      }
    } catch (e: unknown) {
      roundOk = false;
      if ((e as Error).name !== "AbortError") {
        setStreamError((e as Error).message);
      }
    } finally {
      setIsStreaming(false);
      setStreamingRound(null);
      setStreamingStage(null);
      setStreamingProgress(null);
      // Refresh persisted turns from the server so the streaming overlay
      // can be cleared without losing state on the next render. Use
      // refetchQueries (not invalidateQueries) so the round counter +
      // turn list update immediately even if a stale cache window keeps
      // the query "fresh"; otherwise the sidebar can sit on R(N-1) until
      // the user clicks elsewhere.
      queryClient.refetchQueries({ queryKey: ["discussion-session", selectedId] });
      queryClient.refetchQueries({ queryKey: ["discussion-sessions"] });
      // Back-fill exact per-persona cost (+ the persisted breakdown) for
      // the "ctx 用量明細" panel now the round's usage rows are committed.
      queryClient.refetchQueries({
        queryKey: ["discussion-round-usage-detail", selectedId],
      });
      queryClient.refetchQueries({
        queryKey: ["discussion-round-usage", selectedId],
      });
      setStreamingTurns([]);
    }
    return { ok: roundOk, rateLimited };
  }

  async function runRounds() {
    if (!selectedId || isStreaming) return;
    cancelRequestedRef.current = false;
    setCancelling(false);
    const total = roundsPerClick;

    // Pre-flight: each round costs len(persona_ids) AI requests, so a
    // multi-round run needs `total × personaCount` quota. Surface the
    // ceiling up front (with a fresh /auth/me read) so the user can
    // reduce rounds / personas instead of watching the loop die part-way
    // through with a 429. Soft warning only — we still let them proceed
    // and the in-loop rateLimited break stops cleanly when quota runs out.
    const personaCount = personaIds.length;
    if (personaCount > 0) {
      try {
        const { data } = await api.get<{ ai_requests_remaining: number | null }>(
          "/auth/me",
        );
        const remaining = data.ai_requests_remaining;
        if (typeof remaining === "number" && total * personaCount > remaining) {
          const affordable = Math.floor(remaining / personaCount);
          pushToast({
            severity: "warning",
            title: t("discussion.quota_preflight_warn", {
              affordable,
              requested: total,
            }),
          });
        }
      } catch {
        /* /auth/me unavailable — skip the pre-flight, the in-loop
           rateLimited break still protects against runaway requests. */
      }
    }

    setLoopProgress({ current: 0, total });
    try {
      for (let i = 0; i < total; i++) {
        if (cancelRequestedRef.current) break;
        setLoopProgress({ current: i + 1, total });
        // The user explicitly opted into N rounds — always attempt all of
        // them. Per-round soft failures (per-persona LLM error / timeout,
        // transient HTTP error, network blip) still surface via
        // `streamError` but DON'T abort the loop. The one exception is a
        // 429 quota exhaustion: continuing would just hammer the remaining
        // rounds with requests that all 429, so we stop cleanly and tell
        // the user how many rounds actually completed.
        const { rateLimited } = await runOneRound();
        if (rateLimited) {
          pushToast({
            severity: "error",
            title: t("discussion.quota_stopped", {
              completed: i,
              total,
            }),
          });
          break;
        }
      }
    } finally {
      setLoopProgress(null);
      cancelRequestedRef.current = false;
      setCancelling(false);
    }
  }

  function cancelLoop() {
    cancelRequestedRef.current = true;
    setCancelling(true);
  }

  function stopStreaming() {
    abortRef.current?.abort();
  }

  return {
    streamingTurns,
    setStreamingTurns,
    streamingPersona,
    streamBuffer,
    streamingRound,
    streamingStage,
    streamingProgress,
    streamingToolEvents,
    isStreaming,
    streamError,
    setStreamError,
    liveRoundUsage,
    liveRoundUsageDetail,
    roundsPerClick,
    setRoundsPerClick,
    loopProgress,
    cancelling,
    runOneRound,
    runRounds,
    cancelLoop,
    stopStreaming,
  };
}
