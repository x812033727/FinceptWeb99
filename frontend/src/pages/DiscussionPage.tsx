import { useEffect, useMemo, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { notifyRateLimited } from "@/lib/api";
import { useAuthStore } from "@/store/authStore";
import type {
  Discussion,
  DiscussionDetail,
  DiscussionMarket,
  Turn,
} from "@/types/discussion";
import { AutoRunConfigCard } from "@/components/discussion/AutoRunConfigCard";
import { BacktestSweepCard } from "@/components/discussion/BacktestSweepCard";
import { StrategyTemplateCard } from "@/components/discussion/StrategyTemplateCard";
import { ConclusionCard } from "@/components/discussion/ConclusionCard";
import { PostMortemSkippedCard } from "@/components/discussion/PostMortemSkippedCard";
import { PostMortemGainersCard } from "@/components/discussion/PostMortemGainersCard";
import { RoundContextsCard } from "@/components/discussion/RoundContextsCard";
import { RoundSection } from "@/components/discussion/RoundSection";
import { ScoreboardCard } from "@/components/discussion/ScoreboardCard";
import {
  DEFAULT_PERSONAS,
  concludeSession,
  createSession,
  deleteSession,
  injectUserMessage,
  runPostMortem,
  runPostMortemFlowSteps,
  fetchAgents,
  fetchSession,
  fetchSessions,
  formatDateLong,
  formatDateShort,
  formatDiscussionTitle,
  readCollapse,
  readDefaultRules,
  readDefaultTopic,
  readPostMortemResult,
  rememberCollapse,
  rememberPostMortemResult,
  rememberRules,
  rememberTopic,
  renderInlineMarkdown,
  signedPct,
  updateSession,
  usePersonaName,
} from "@/components/discussion/_helpers";
import type { PostMortemResponse } from "@/components/discussion/_helpers";
import type { CollapseState } from "@/components/discussion/_helpers";

export default function DiscussionPage() {
  const { t } = useTranslation();
  const token = useAuthStore((s) => s.token);
  const queryClient = useQueryClient();

  const { data: agents = [] } = useQuery({
    queryKey: ["ai-agents"],
    queryFn: fetchAgents,
  });

  const { data: sessions = [] } = useQuery({
    queryKey: ["discussion-sessions"],
    queryFn: fetchSessions,
  });

  const [selectedId, setSelectedId] = useState<string | null>(null);

  // Local form state for the active session's editable fields.
  // Read the last-saved topic / rules from localStorage on mount so a
  // new discussion picks up where the user left off. Falls back to the
  // hardcoded DEFAULTs on first ever use (or if localStorage is
  // disabled).
  const [topic, setTopic] = useState(readDefaultTopic);
  const [rules, setRules] = useState(readDefaultRules);

  // Per-section collapse state, persisted to localStorage so the user's
  // layout preference survives reload / new tab.
  const [collapse, setCollapse] = useState<CollapseState>(readCollapse);
  useEffect(() => {
    rememberCollapse(collapse);
  }, [collapse]);

  function toggleCollapse(key: keyof CollapseState) {
    setCollapse((prev) => ({ ...prev, [key]: !prev[key] }));
  }
  const [personaIds, setPersonaIds] = useState<string[]>(DEFAULT_PERSONAS);
  const [market, setMarket] = useState<DiscussionMarket>("TW");
  // Backtest anchor (PR #224). Empty string = live mode. ISO date
  // ("2025-01-15") = "pretend it's that date" — backend fetches
  // historical-only ctx + verifier grades against next 5 trading
  // days from this anchor.
  const [asOfDate, setAsOfDate] = useState<string>("");

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
  const [isStreaming, setIsStreaming] = useState(false);
  const [streamError, setStreamError] = useState<string | null>(null);
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
  const bottomRef = useRef<HTMLDivElement>(null);

  const { data: detail } = useQuery<DiscussionDetail>({
    queryKey: ["discussion-session", selectedId],
    queryFn: () => fetchSession(selectedId!),
    enabled: !!selectedId,
  });

  const personaName = usePersonaName(agents);

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
    setStreamError(null);
    setStreamingToolEvents([]);
  }, [selectedId]);

  // Hydrate the editable form fields from the active session's detail
  // exactly once per session pick. A ref guards against re-hydrating on
  // every TanStack Query refetch (which would clobber whatever the user
  // just typed into the textarea).
  const hydratedForId = useRef<string | null>(null);
  useEffect(() => {
    if (!detail) return;
    if (hydratedForId.current === detail.id) return;
    hydratedForId.current = detail.id;
    setTopic(detail.topic);
    setRules(detail.rules);
    setPersonaIds(detail.persona_ids);
    setMarket(detail.market ?? "TW");
    setAsOfDate(detail.as_of_date ?? "");
  }, [detail]);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [detail?.turns, streamingTurns, streamBuffer]);

  // Combined transcript: persisted turns + the round currently streaming.
  const transcript: Turn[] = useMemo(() => {
    const persisted = detail?.turns ?? [];
    return [...persisted, ...streamingTurns];
  }, [detail?.turns, streamingTurns]);

  // ── mutations ─────────────────────────────────────────────────

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
  });

  const deleteMut = useMutation({
    mutationFn: deleteSession,
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["discussion-sessions"] });
      setSelectedId(null);
    },
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
      runRound,
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

  // ── round streaming ──────────────────────────────────────────

  function togglePersona(id: string) {
    setPersonaIds((prev) =>
      prev.includes(id) ? prev.filter((p) => p !== id) : [...prev, id],
    );
  }

  function newDiscussion() {
    if (personaIds.length < 2) {
      setStreamError("請選擇至少 2 位專家");
      return;
    }
    if (personaIds.length > 8) {
      setStreamError("最多選擇 8 位專家");
      return;
    }
    setStreamError(null);
    createMut.mutate({
      topic, rules, persona_ids: personaIds, market,
      as_of_date: asOfDate || undefined,
    });
  }

  function saveEdits() {
    if (!selectedId) return;
    updateMut.mutate({ topic, rules, persona_ids: personaIds, market });
  }

  // Per-field saves so the user can commit just the topic edit without
  // also locking in pending persona-toggle changes (and vice versa).
  // Backend's PATCH endpoint accepts partial bodies — fields not in the
  // payload stay untouched.
  const topicDirty = !!selectedId && topic !== (detail?.topic ?? "");
  const rulesDirty = !!selectedId && rules !== (detail?.rules ?? "");

  function saveTopic() {
    if (!selectedId || !topicDirty) return;
    updateMut.mutate({ topic });
  }
  function saveRules() {
    if (!selectedId || !rulesDirty) return;
    updateMut.mutate({ rules });
  }

  async function runRound() {
    if (!selectedId || isStreaming) return;
    setIsStreaming(true);
    setStreamError(null);
    setStreamingTurns([]);
    setStreamBuffer("");
    setStreamingPersona(null);
    setStreamingRound(null);
    setStreamingStage(null);

    const ctrl = new AbortController();
    abortRef.current = ctrl;

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
                setStreamBuffer("");
                setStreamingPersona(null);
                setStreamingToolEvents([]);
                currentBuffer = "";
                break;
              case "error":
                setStreamError(obj.message ?? "未知錯誤");
                break;
            }
          } catch {
            /* malformed event — skip */
          }
        }
      }
    } catch (e: unknown) {
      if ((e as Error).name !== "AbortError") {
        setStreamError((e as Error).message);
      }
    } finally {
      setIsStreaming(false);
      setStreamingRound(null);
      setStreamingStage(null);
      // Refresh persisted turns from the server so the streaming overlay
      // can be cleared without losing state on the next render. Use
      // refetchQueries (not invalidateQueries) so the round counter +
      // turn list update immediately even if a stale cache window keeps
      // the query "fresh"; otherwise the sidebar can sit on R(N-1) until
      // the user clicks elsewhere.
      queryClient.refetchQueries({ queryKey: ["discussion-session", selectedId] });
      queryClient.refetchQueries({ queryKey: ["discussion-sessions"] });
      setStreamingTurns([]);
    }
  }

  function stopStreaming() {
    abortRef.current?.abort();
  }

  const status = detail?.status ?? "draft";
  const isDraft = !selectedId || status === "draft";

  // ── render ────────────────────────────────────────────────────

  return (
    <div className="h-[calc(100vh-2.5rem)] bg-background flex flex-col lg:flex-row overflow-hidden">
      {/* ── sidebar: session list + form ───────────────────────── */}
      <aside
        className={`lg:w-60 border-b lg:border-b-0 lg:border-r border-border flex-col p-3 lg:p-3 gap-3 shrink-0 overflow-y-auto max-h-[28vh] lg:max-h-none ${
          collapse.sidebar ? "hidden" : "flex"
        }`}
      >
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

        {/* PR-A: strategy template manager. The sweep card below
            reads from the same query so saving here makes a new
            entry appear in the sweep form's picker immediately. */}
        <StrategyTemplateCard
          prefill={{
            topic,
            rules,
            market,
            personaIdsCsv: personaIds.join(", "),
          }}
        />

        {/* PR #274: multi-day backtest sweep panel. Reuses the
            current form's topic/rules/personas as the template
            applied to every spawned discussion in the sweep. */}
        <BacktestSweepCard
          topic={topic}
          rules={rules}
          market={market}
          personaIds={personaIds}
        />

        <button
          onClick={() => {
            setSelectedId(null);
            // "+ New Discussion" pulls from localStorage so the user's
            // last-saved topic / rules are pre-filled.
            setTopic(readDefaultTopic());
            setRules(readDefaultRules());
            setPersonaIds(DEFAULT_PERSONAS);
            setStreamingTurns([]);
            setStreamError(null);
          }}
          className="px-3 py-2 rounded-md border border-border text-xs hover:border-primary/40 hover:bg-accent/10 transition-colors text-left"
        >
          + {t("discussion.new")}
        </button>

        <div className="space-y-1">
          {sessions.length === 0 && (
            <p className="text-xs text-muted-foreground">{t("discussion.empty")}</p>
          )}
          {sessions.map((s) => (
            <button
              key={s.id}
              onClick={() => setSelectedId(s.id)}
              className={`w-full text-left px-2 py-2 rounded text-xs transition-colors ${
                selectedId === s.id
                  ? "bg-primary/15 text-primary"
                  : "hover:bg-accent/10 text-muted-foreground"
              }`}
            >
              {(() => {
                const tt = formatDiscussionTitle(s);
                if (tt.text !== undefined) {
                  // No conclusion yet — fall back to the user-typed topic.
                  return (
                    <div className="line-clamp-2 font-bold text-foreground">
                      {tt.text}
                    </div>
                  );
                }
                // Conclusion present: date header + per-symbol lines.
                return (
                  <div className="space-y-0.5">
                    <div className={`font-bold ${tt.verdictCls ?? ""}`}>
                      {tt.date}
                      {tt.verdictMark ? ` ${tt.verdictMark}` : ""}
                    </div>
                    {tt.lines?.map((ln) => (
                      <div key={ln.symbol} className={`font-mono ${ln.cls}`}>
                        {ln.symbol}:{" "}
                        {ln.changePcts.map((p, i) => (
                          <span key={i}>
                            {p !== null ? signedPct(p) : "—"}
                            {i < ln.changePcts.length - 1 ? "/" : ""}
                          </span>
                        ))}
                      </div>
                    ))}
                  </div>
                );
              })()}
              <div className="mt-0.5 flex items-center gap-2 text-[10px]">
                {/* PR #277: backtest rows show their `as_of_date`
                    (the historical day being analysed) rather than
                    when the row happened to be created/updated.
                    Live rows keep the existing updated_at/created_at
                    behaviour. */}
                <span>
                  {s.as_of_date
                    ? `回測 ${s.as_of_date}`
                    : formatDateShort(s.updated_at || s.created_at)}
                </span>
                <span>·</span>
                <span>R{s.current_round}</span>
                <span>·</span>
                <span>{s.persona_ids.length} 位專家</span>
                <span>·</span>
                <span>{t(`discussion.status.${s.status}`)}</span>
              </div>
            </button>
          ))}
        </div>

        <div className="mt-auto text-xs text-muted-foreground">
          <a href="/dashboard" className="hover:text-foreground transition-colors">
            {t("ai.back_dashboard")}
          </a>
        </div>
      </aside>

      {/* ── main area ──────────────────────────────────────────── */}
      <div className="flex-1 flex flex-col min-w-0 min-h-0">
        {/* sidebar toggle — single chevron button at top-left of main
            area. Click to hide / show the discussion list. State is
            persisted so it survives reload. */}
        <button
          type="button"
          onClick={() => toggleCollapse("sidebar")}
          title={
            collapse.sidebar
              ? t("discussion.show_menu")
              : t("discussion.hide_menu")
          }
          className="self-start mt-2 ml-2 px-1.5 py-0.5 text-[11px] text-muted-foreground border border-border rounded hover:border-primary/40 hover:text-foreground transition-colors"
        >
          {collapse.sidebar
            ? `›  ${t("discussion.menu")}`
            : `‹  ${t("discussion.menu")}`}
        </button>
        {/* configuration panel — capped height so the transcript below
             always has room. Mobile cap is tighter because the sidebar
             above still claims up to 28vh, and the actions / streaming
             cards need to stay visible. */}
        <div className="border-b border-border px-4 py-3 space-y-3 shrink-0 overflow-y-auto max-h-[40vh] lg:max-h-[60vh]">
          <div>
            <div className="flex items-center justify-between gap-2">
              <button
                type="button"
                onClick={() => toggleCollapse("topic")}
                className="flex items-center gap-1.5 text-xs font-medium text-foreground hover:text-primary transition-colors"
                aria-expanded={!collapse.topic}
              >
                <span className="text-[9px] text-muted-foreground w-2.5 inline-block">
                  {collapse.topic ? "▶" : "▼"}
                </span>
                {t("discussion.topic_label")}
                {topicDirty && (
                  <span className="ml-1 text-[10px] text-amber-400">
                    {t("discussion.unsaved")}
                  </span>
                )}
              </button>
              {selectedId && isDraft && (
                <button
                  onClick={saveTopic}
                  disabled={!topicDirty || updateMut.isPending || isStreaming}
                  className="px-2 py-0.5 text-[10px] border border-border rounded hover:border-primary/40 transition-colors disabled:opacity-30"
                >
                  {updateMut.isPending ? t("common.saving") : t("common.save")}
                </button>
              )}
            </div>
            {collapse.topic ? (
              topic && (
                <p className="mt-1 ml-4 text-[11px] text-muted-foreground line-clamp-1">
                  {topic}
                </p>
              )
            ) : (
              <textarea
                value={topic}
                onChange={(e) => setTopic(e.target.value)}
                disabled={!isDraft || isStreaming}
                rows={2}
                maxLength={500}
                className="w-full mt-1 resize-none bg-card border border-border rounded-md px-3 py-2 text-sm text-foreground focus:outline-none focus:border-primary/50 disabled:opacity-60"
              />
            )}
          </div>

          <div>
            <div className="flex items-center justify-between gap-2">
              <button
                type="button"
                onClick={() => toggleCollapse("rules")}
                className="flex items-center gap-1.5 text-xs font-medium text-foreground hover:text-primary transition-colors"
                aria-expanded={!collapse.rules}
              >
                <span className="text-[9px] text-muted-foreground w-2.5 inline-block">
                  {collapse.rules ? "▶" : "▼"}
                </span>
                {t("discussion.rules_label")}
                {rulesDirty && (
                  <span className="ml-1 text-[10px] text-amber-400">
                    {t("discussion.unsaved")}
                  </span>
                )}
              </button>
              {selectedId && isDraft && (
                <button
                  onClick={saveRules}
                  disabled={!rulesDirty || updateMut.isPending || isStreaming}
                  className="px-2 py-0.5 text-[10px] border border-border rounded hover:border-primary/40 transition-colors disabled:opacity-30"
                >
                  {updateMut.isPending ? t("common.saving") : t("common.save")}
                </button>
              )}
            </div>
            {collapse.rules ? (
              rules && (
                <p className="mt-1 ml-4 text-[11px] text-muted-foreground line-clamp-1">
                  {rules.split("\n")[0]}
                </p>
              )
            ) : (
              <textarea
                value={rules}
                onChange={(e) => setRules(e.target.value)}
                disabled={!isDraft || isStreaming}
                rows={5}
                maxLength={2000}
                className="w-full mt-1 resize-none bg-card border border-border rounded-md px-3 py-2 text-xs text-foreground font-mono focus:outline-none focus:border-primary/50 disabled:opacity-60"
              />
            )}
          </div>

          <div>
            <button
              type="button"
              onClick={() => toggleCollapse("personas")}
              className="flex items-center gap-1.5 text-xs font-medium text-foreground hover:text-primary transition-colors"
              aria-expanded={!collapse.personas}
            >
              <span className="text-[9px] text-muted-foreground w-2.5 inline-block">
                {collapse.personas ? "▶" : "▼"}
              </span>
              {t("discussion.personas_label")} ({personaIds.length})
            </button>
            {!collapse.personas && (
              <div className="mt-1 flex flex-wrap gap-1.5">
                {agents.map((a) => {
                  const selected = personaIds.includes(a.id);
                  return (
                    <button
                      key={a.id}
                      onClick={() => togglePersona(a.id)}
                      disabled={!isDraft || isStreaming}
                      className={`px-2 py-1 rounded text-[11px] border transition-colors disabled:opacity-60 ${
                        selected
                          ? "border-primary bg-primary/15 text-primary"
                          : "border-border bg-card text-muted-foreground hover:border-primary/40"
                      }`}
                    >
                      {personaName(a.id)}
                    </button>
                  );
                })}
              </div>
            )}
          </div>

          <div className="flex items-center gap-2 text-xs flex-wrap">
            <span className="text-muted-foreground">{t("discussion.market_label")}</span>
            <select
              value={market}
              onChange={(e) => setMarket(e.target.value as DiscussionMarket)}
              disabled={!isDraft || isStreaming}
              className="bg-card border border-border rounded px-2 py-1 text-foreground focus:outline-none focus:border-primary/50 disabled:opacity-60"
            >
              <option value="TW">TW</option>
              <option value="US">US</option>
              <option value="GLOBAL">GLOBAL</option>
            </select>
            <span className="text-muted-foreground ml-2">
              {t("discussion.as_of_label")}
            </span>
            <input
              type="date"
              value={asOfDate}
              onChange={(e) => setAsOfDate(e.target.value)}
              disabled={!!selectedId || isStreaming}
              max={new Date().toISOString().slice(0, 10)}
              placeholder={t("discussion.as_of_placeholder")}
              className="bg-card border border-border rounded px-2 py-1 text-foreground focus:outline-none focus:border-primary/50 disabled:opacity-60"
            />
            {asOfDate && (
              <span className="px-1.5 py-0.5 rounded text-[10px] border border-amber-800/50 bg-amber-900/20 text-amber-300">
                {t("discussion.backtest_badge")}
              </span>
            )}
          </div>

          <div className="flex items-center gap-2 flex-wrap">
            {!selectedId ? (
              <button
                onClick={newDiscussion}
                disabled={createMut.isPending}
                className="px-4 py-2 rounded-md bg-primary text-primary-foreground text-sm font-medium hover:bg-primary/90 transition-colors disabled:opacity-50"
              >
                {createMut.isPending ? t("common.saving") : t("discussion.create")}
              </button>
            ) : (
              <>
                {isDraft && (
                  <button
                    onClick={saveEdits}
                    disabled={updateMut.isPending}
                    className="px-3 py-1.5 rounded-md border border-border text-xs hover:border-primary/40 transition-colors disabled:opacity-50"
                  >
                    {updateMut.isPending ? t("common.saving") : t("discussion.save_edits")}
                  </button>
                )}
                {isStreaming ? (
                  <button
                    onClick={stopStreaming}
                    className="px-3 py-1.5 rounded-md bg-red-900/30 border border-red-800 text-red-400 text-xs hover:bg-red-900/50 transition-colors"
                  >
                    {t("ai.stop")}
                  </button>
                ) : (
                  <button
                    onClick={runRound}
                    className="px-4 py-2 rounded-md bg-primary text-primary-foreground text-sm font-medium hover:bg-primary/90 transition-colors"
                  >
                    {detail?.current_round
                      ? t("discussion.next_round", { round: detail.current_round + 1 })
                      : t("discussion.start_round")}
                  </button>
                )}
                <button
                  onClick={() => concludeMut.mutate()}
                  disabled={
                    isStreaming ||
                    concludeMut.isPending ||
                    (detail?.current_round ?? 0) === 0
                  }
                  className="px-3 py-1.5 rounded-md border border-amber-800/50 text-amber-300 text-xs hover:bg-amber-900/20 transition-colors disabled:opacity-50"
                >
                  {concludeMut.isPending ? t("common.computing") : t("discussion.conclude")}
                </button>
                {/* Post-mortem self-critique button — backtest + has-
                    conclusion only. Chains: inject critique prompt →
                    run new round → re-conclude. Hidden in live mode
                    since there's no ground truth to critique against. */}
                {detail?.as_of_date && detail?.conclusion && (
                  <button
                    onClick={runPostMortemFlow}
                    disabled={
                      isStreaming ||
                      postMortemMut.isPending ||
                      concludeMut.isPending
                    }
                    title={t("discussion.post_mortem_hint")}
                    className="px-3 py-1.5 rounded-md border border-purple-800/50 text-purple-300 text-xs hover:bg-purple-900/20 transition-colors disabled:opacity-50"
                  >
                    {postMortemMut.isPending
                      ? t("discussion.post_mortem_running")
                      : t("discussion.post_mortem")}
                  </button>
                )}
                <button
                  onClick={() => deleteMut.mutate(selectedId)}
                  disabled={deleteMut.isPending || isStreaming}
                  className="px-3 py-1.5 rounded-md border border-border text-xs text-muted-foreground hover:text-red-400 hover:border-red-800/50 transition-colors disabled:opacity-50"
                >
                  {t("common.delete")}
                </button>
              </>
            )}
          </div>
          {selectedId && isDraft && (detail?.current_round ?? 0) >= 1 && !isStreaming && (
            <div className="border border-border rounded-md p-2 bg-card/40 space-y-1.5">
              <label className="text-[11px] text-muted-foreground">
                {t("discussion.inject_label")}
              </label>
              <textarea
                value={injectDraft}
                onChange={(e) => setInjectDraft(e.target.value)}
                onKeyDown={(e) => {
                  // Cmd+Enter (macOS) / Ctrl+Enter (Win/Linux) submits.
                  // Bare Enter still inserts a newline so multi-line
                  // injections aren't accidentally sent on the first
                  // line break.
                  if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) {
                    e.preventDefault();
                    const trimmed = injectDraft.trim();
                    if (trimmed && !injectMut.isPending) {
                      injectMut.mutate(trimmed);
                    }
                  }
                }}
                rows={2}
                maxLength={2000}
                placeholder={t("discussion.inject_placeholder")}
                className="w-full resize-none bg-card border border-border rounded px-2 py-1 text-xs text-foreground focus:outline-none focus:border-primary/50"
              />
              <div className="flex items-center justify-between">
                <span className="text-[10px] text-muted-foreground">
                  {injectDraft.length}/2000
                  <span className="ml-2 opacity-60">
                    {t("discussion.inject_shortcut_hint")}
                  </span>
                </span>
                <button
                  type="button"
                  onClick={() => injectMut.mutate(injectDraft.trim())}
                  disabled={!injectDraft.trim() || injectMut.isPending}
                  className="px-2.5 py-1 rounded text-[11px] border border-amber-800/50 text-amber-300 hover:bg-amber-900/20 transition-colors disabled:opacity-40"
                >
                  {injectMut.isPending ? t("common.saving") : t("discussion.inject_send")}
                </button>
              </div>
              {injectMut.isError && (
                <p className="text-[10px] text-red-400">
                  {(injectMut.error as Error)?.message ?? t("common.error")}
                </p>
              )}
            </div>
          )}
          {streamError && (
            <div className="text-xs text-red-400 bg-red-950/30 border border-red-900/50 rounded px-3 py-2">
              {streamError}
            </div>
          )}
        </div>

        {/* transcript */}
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
            return orderedRounds.map((rn) => (
              <RoundSection
                key={rn}
                discussionId={selectedId}
                round={rn}
                turns={byRound.get(rn) ?? []}
                personaName={personaName}
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
                      if (streamingStage) {
                        const i18nKey = `discussion.loading_stage_${streamingStage}`;
                        const translated = t(i18nKey);
                        if (translated !== i18nKey) return translated;
                      }
                      return streamingRound === null
                        ? t("discussion.loading_context_initial")
                        : t("discussion.loading_context");
                    })()}
                  </span>
                </div>
              </div>
            </>
          )}
          {isStreaming && streamingPersona && (
            <>
              {/* When the streaming round hasn't appeared in the transcript
                  yet (first persona of a fresh round), show the round
                  header above the in-progress card so the user always
                  knows which round is being generated. */}
              {streamingRound !== null &&
                (transcript.length === 0 ||
                  transcript[transcript.length - 1].round !== streamingRound) && (
                  <div className="flex items-center gap-2 my-3">
                    <span className="text-[11px] font-semibold text-primary tracking-wider">
                      {t("discussion.round_label", { round: streamingRound })}
                    </span>
                    <span className="flex-1 h-px bg-border" />
                  </div>
                )}
              <div className="bg-card border border-primary/40 rounded-lg p-3">
                <div className="flex items-center gap-2 text-xs text-muted-foreground mb-1">
                  <span className="font-bold text-red-500">
                    {personaName(streamingPersona)}
                  </span>
                  {streamingRound !== null && (
                    <>
                      <span>·</span>
                      <span>R{streamingRound}</span>
                    </>
                  )}
                  <span>·</span>
                  <span className="animate-pulse">{t("discussion.thinking")}</span>
                </div>
                {streamingToolEvents.length > 0 && (
                  <div className="mb-2 space-y-0.5 font-mono text-[10px] text-muted-foreground">
                    {streamingToolEvents.map((ev) => {
                      const argsStr = ev.args !== undefined
                        ? JSON.stringify(ev.args)
                        : "";
                      const sumStr = (ev.summary ?? "").replace(/\s+/g, " ").trim();
                      const truncate = (s: string, max: number) =>
                        s.length > max ? s.slice(0, max) + "…" : s;
                      const icon = ev.is_error ? "⚠️" : ev.kind === "result" ? "✓" : "⏳";
                      const tone = ev.is_error
                        ? "text-amber-400"
                        : ev.kind === "result"
                        ? "text-emerald-400"
                        : "text-muted-foreground";
                      return (
                        <div key={ev.id} className={`flex gap-1 ${tone}`}>
                          <span className="shrink-0">{icon}</span>
                          <span className="truncate">
                            <span className="font-semibold">{ev.name}</span>
                            {argsStr && (
                              <span className="opacity-70"> {truncate(argsStr, 60)}</span>
                            )}
                            {sumStr && (
                              <span className="opacity-90"> → {truncate(sumStr, 80)}</span>
                            )}
                          </span>
                        </div>
                      );
                    })}
                  </div>
                )}
                <div className="text-sm text-foreground whitespace-pre-wrap leading-relaxed">
                  {renderInlineMarkdown(streamBuffer)}
                  <span className="inline-block w-1.5 h-3.5 bg-current ml-0.5 animate-pulse align-middle" />
                </div>
              </div>
            </>
          )}

          {detail?.conclusion && (
            <ConclusionCard detail={detail} personaName={personaName} />
          )}
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
      </div>
    </div>
  );
}
