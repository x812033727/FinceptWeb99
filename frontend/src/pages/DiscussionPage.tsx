import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useVirtualizer } from "@tanstack/react-virtual";
import {
  ChevronDown,
  ChevronRight,
  Download,
  Folder,
  MoreHorizontal,
  Search,
  Settings as SettingsIcon,
  Sparkles,
  Trash2,
} from "lucide-react";
import api, { errorDetail, notifyRateLimited } from "@/lib/api";
import { useAuthStore } from "@/store/authStore";
import { useToastStore } from "@/store/toastStore";
import { cn } from "@/lib/utils";
import { useCollapsible as useCollapsibleHook } from "@/hooks/useCollapsible";
import type {
  Discussion,
  DiscussionDetail,
  DiscussionMarket,
  Turn,
} from "@/types/discussion";
import { AutoRunConfigCard } from "@/components/discussion/AutoRunConfigCard";
import { BacktestSweepCard } from "@/components/discussion/BacktestSweepCard";
import { DiscussionStatusBadge } from "@/components/discussion/DiscussionStatusBadge";
import { DiscussionToolbar } from "@/components/discussion/DiscussionToolbar";
import { StrategyTemplateCard } from "@/components/discussion/StrategyTemplateCard";
import { ConclusionCard } from "@/components/discussion/ConclusionCard";
import { ConclusionDiffCard } from "@/components/discussion/ConclusionDiffCard";
import { PostMortemSkippedCard } from "@/components/discussion/PostMortemSkippedCard";
import { PostMortemGainersCard } from "@/components/discussion/PostMortemGainersCard";
import { RoundContextsCard } from "@/components/discussion/RoundContextsCard";
import { RoundDivider } from "@/components/discussion/RoundDivider";
import { RoundSection } from "@/components/discussion/RoundSection";
import { ScoreboardCard } from "@/components/discussion/ScoreboardCard";
import { StreamingTurnCard } from "@/components/discussion/StreamingTurnCard";
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
} from "@/components/ui/sheet";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import {
  concludeSession,
  createSession,
  deleteSession,
  downloadDiscussionMarkdown,
  injectUserMessage,
  runPostMortem,
  runPostMortemFlowSteps,
  fetchAgents,
  fetchScoreboard,
  fetchSession,
  fetchSessions,
  BAND_LABELS,
  formatDateLong,
  formatDateShort,
  formatDiscussionTitle,
  pctClass,
  readCollapse,
  readDefaultMarket,
  readDefaultPersonas,
  readDefaultRules,
  readDefaultTopic,
  readPostMortemResult,
  readRoundsPerClick,
  rememberCollapse,
  rememberDiscussionDefaults,
  rememberPostMortemResult,
  rememberRoundsPerClick,
  rememberRules,
  rememberTopic,
  signedPct,
  updateSession,
  usePersonaName,
  usePersonaShort,
} from "@/components/discussion/_helpers";
import type { PostMortemResponse } from "@/components/discussion/_helpers";
import type { CollapseState } from "@/components/discussion/_helpers";

// Lazy on-demand fallback for the sidebar scoreboard. The persisted
// `daily_close_prices` column on Discussion is filled by the daily
// `score_discussion_outcomes` cron (09:30 UTC). When that cron lags
// — or for same-day concluded discussions whose anchor is in the
// past but the cron hasn't ticked yet — the column is NULL and the
// row would otherwise render "-/-/-/-/-" indefinitely. The detail
// page's `/scoreboard` endpoint computes the same payload on demand
// (with a live OHLCV waterfall fallback), so we fire it here per row
// and merge into the `formatDiscussionTitle` input. Top-level
// component so each row keeps its own `useQuery` instance (hook
// rules + ESLint `react-hooks/static-components`).
function SessionRowBody({ s }: { s: Discussion }) {
  const todayIso = new Date().toISOString().slice(0, 10);
  const anchorIso = s.as_of_date ?? s.created_at?.slice(0, 10) ?? null;
  const anchorReached = anchorIso ? anchorIso <= todayIso : false;
  const needsLazy =
    !s.daily_close_prices &&
    !!s.conclusion?.recommended_symbols?.length &&
    anchorReached;

  const { data: lazy } = useQuery({
    queryKey: ["discussion-scoreboard-lazy", s.id],
    queryFn: () => fetchScoreboard(s.id),
    enabled: needsLazy,
    staleTime: 60 * 60 * 1000,
    retry: false,
  });

  const merged = useMemo(() => {
    if (!lazy) return s;
    const dailyDict: Record<string, (number | null)[]> = {
      ...(s.daily_close_prices ?? {}),
    };
    const opensDict: Record<string, number> = { ...(s.day1_open_prices ?? {}) };
    for (const r of lazy.rows ?? []) {
      if (Array.isArray(r.daily_closes)) dailyDict[r.symbol] = r.daily_closes;
      if (typeof r.day1_open === "number") opensDict[r.symbol] = r.day1_open;
    }
    return {
      ...s,
      daily_close_prices: Object.keys(dailyDict).length ? dailyDict : null,
      day1_open_prices: Object.keys(opensDict).length ? opensDict : null,
    };
  }, [s, lazy]);

  const tt = formatDiscussionTitle(merged);
  return (
    <>
      {tt.text !== undefined ? (
        <div className="line-clamp-2 font-bold text-foreground">{tt.text}</div>
      ) : (
        <div className="space-y-0.5">
          <div className="font-bold">{tt.date}</div>
          {tt.lines?.map((ln) => {
            const bandLabel = ln.band ? BAND_LABELS[ln.band] : undefined;
            return (
              <div key={ln.symbol} className="font-mono">
                <span className={bandLabel?.cls ?? ""}>{ln.symbol}</span>
                {bandLabel?.mark ? (
                  <span className={`ml-1 text-[10px] ${bandLabel.cls}`}>
                    {bandLabel.mark}
                  </span>
                ) : null}
                :{" "}
                {ln.changePcts.map((p, i) => (
                  <span key={i}>
                    <span className={pctClass(p)}>
                      {p !== null ? signedPct(p) : "—"}
                    </span>
                    {i < ln.changePcts.length - 1 ? "/" : ""}
                  </span>
                ))}
              </div>
            );
          })}
        </div>
      )}
      <div className="mt-1 flex items-center gap-2 text-[10px] flex-wrap">
        <DiscussionStatusBadge status={s.status} />
        <span className="text-muted-foreground">
          {s.as_of_date
            ? `回測 ${s.as_of_date}`
            : formatDateShort(s.updated_at || s.created_at)}
        </span>
        <span className="text-muted-foreground">·</span>
        <span className="text-muted-foreground">R{s.current_round}</span>
        <span className="text-muted-foreground">·</span>
        <span className="text-muted-foreground">
          {s.persona_ids.length} 位專家
        </span>
      </div>
    </>
  );
}

export default function DiscussionPage() {
  const { t } = useTranslation();
  const token = useAuthStore((s) => s.token);
  const queryClient = useQueryClient();
  const pushToast = useToastStore((s) => s.push);

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
  const [personaIds, setPersonaIds] = useState<string[]>(readDefaultPersonas);
  const [market, setMarket] = useState<DiscussionMarket>(readDefaultMarket);
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
  // C1-3: when the per-symbol news fan-out reports a counter,
  // surface it as "Scoring news sentiment 3/5" in the preparing
  // card. Null when the current stage doesn't ship done/total
  // (every other phase, or a backend that pre-dates C1-3).
  const [streamingProgress, setStreamingProgress] = useState<
    { done: number; total: number } | null
  >(null);
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
  const bottomRef = useRef<HTMLDivElement>(null);
  // Virtualization parent for the desktop sessions rail. Keeps the
  // DOM small (~30 rows) regardless of how many discussions the user
  // has accumulated; without this, a 200-row archive renders 200
  // backtest scoreboard cells on every render.
  const sessionsScrollRef = useRef<HTMLDivElement>(null);

  const { data: detail } = useQuery<DiscussionDetail>({
    queryKey: ["discussion-session", selectedId],
    queryFn: () => fetchSession(selectedId!),
    enabled: !!selectedId,
  });

  const personaName = usePersonaName(agents);
  const personaShort = usePersonaShort();

  // Reset all per-session streaming state when the user switches to a
  // different discussion. Keyed on `selectedId` (not `detail`) so the
  // optimistic cache mutations during a round don't nuke the streaming
  // overlay mid-stream.
  useEffect(() => {
    setStreamingTurns([]);
    setStreamBuffer("");
    setStreamingPersona(null);
    setStreamingRound(null);
    setStreamingStage(null);
    setStreamingProgress(null);
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

  // Mobile drawer state for the redesign (PR-B). Sessions list and
  // config panel are full-screen drawers below the lg breakpoint so
  // the transcript owns the phone viewport. Inject panel pops as a
  // dialog from the bottom action bar's "More" menu.
  const [sessionsSheetOpen, setSessionsSheetOpen] = useState(false);
  const [configSheetOpen, setConfigSheetOpen] = useState(false);
  const [injectSheetOpen, setInjectSheetOpen] = useState(false);

  // Desktop layout state (PR-D). Sticky action bar + collapsible
  // config bar replace the old top-pinned `max-h-[60vh]` config
  // panel that ate vertical space and pushed buttons off-screen.
  // The config bar's open state persists per-user via localStorage
  // so people who prefer "form first, transcript second" don't have
  // to expand it on every visit.
  const desktopConfig = useCollapsibleHook("discussion.desktop_config", true);

  // Sessions sidebar filter state (PR-D). Search matches topic +
  // recommended_symbols (case-insensitive); status pill narrows by
  // draft / running / done.
  const [sessionsQuery, setSessionsQuery] = useState("");
  const [sessionsStatusFilter, setSessionsStatusFilter] =
    useState<"all" | "draft" | "running" | "done">("all");

  // Sessions list filter — search query matches against both the
  // topic and any recommended_symbols on the conclusion (so someone
  // who remembers the picks but not the topic can still find the
  // discussion). Status filter narrows by draft / running / done.
  const filteredSessions = (() => {
    const q = sessionsQuery.trim().toLowerCase();
    return sessions.filter((s) => {
      if (sessionsStatusFilter !== "all" && s.status !== sessionsStatusFilter) {
        return false;
      }
      if (!q) return true;
      if (s.topic.toLowerCase().includes(q)) return true;
      const recs = s.conclusion?.recommended_symbols ?? [];
      return recs.some((sym) => sym.toLowerCase().includes(q));
    });
  })();

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

  // Snapshot the current config-form values as the user's per-browser
  // defaults. Fires from the form's footer button — not tied to a
  // create / update mutation, so the user can lock in a persona roster
  // + market choice without first creating a discussion. as_of_date is
  // intentionally excluded (it's a per-backtest anchor, not something
  // anyone wants as a "default").
  function saveAsDefaults() {
    rememberDiscussionDefaults({ topic, rules, personaIds, market });
    pushToast({
      severity: "success",
      title: t("discussion.save_as_defaults_done"),
    });
  }

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
                setStreamBuffer("");
                setStreamingPersona(null);
                setStreamingToolEvents([]);
                currentBuffer = "";
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

  const status = detail?.status ?? "draft";
  const isDraft = !selectedId || status === "draft";

  // ── render helpers — reusable JSX blocks shared between the
  //    desktop inline layout and the mobile Sheet drawers ──────

  // Reset to a fresh draft. Used by the desktop toolbar's
  // 「+ 新討論」 button AND the mobile sidebar drawer's same
  // button — `useCallback` so `<DiscussionToolbar>` doesn't
  // remount its popovers when unrelated state changes.
  const handleNewDiscussion = useCallback(() => {
    setSelectedId(null);
    setTopic(readDefaultTopic());
    setRules(readDefaultRules());
    setPersonaIds(readDefaultPersonas());
    setMarket(readDefaultMarket());
    setStreamingTurns([]);
    setStreamError(null);
    setSessionsSheetOpen(false);
  }, []);

  // Mobile sidebar drawer — keeps the original layout (header + 3
  // tool cards + new-discussion button + sessions filter + list).
  // Desktop uses `renderSessionsRail()` only and surfaces tools via
  // the new horizontal toolbar above the transcript.
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

  function renderSessionsFilter() {
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

  // Mobile drawer body: tools + filter + list as one scroll. Desktop
  // splits these into the toolbar (tools) and sidebar (filter + list).
  function renderSidebarContent() {
    return (
      <>
        {renderSidebarTools()}
        {renderSessionsFilter()}
        {renderSessionsListNonVirtualized()}
        <div className="mt-auto text-xs text-muted-foreground pt-2">
          <a href="/dashboard" className="hover:text-foreground transition-colors">
            {t("ai.back_dashboard")}
          </a>
        </div>
      </>
    );
  }

  function renderConfigForm() {
    return (
      <>
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
                    className={cn(
                      "px-2 py-1 rounded text-[11px] border transition-colors disabled:opacity-60 min-h-[32px]",
                      selected
                        ? "border-primary bg-primary/15 text-primary"
                        : "border-border bg-card text-muted-foreground hover:border-primary/40"
                    )}
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
            className="bg-card border border-border rounded px-2 py-1 text-foreground focus:outline-none focus:border-primary/50 disabled:opacity-60 min-h-[32px]"
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
            className="bg-card border border-border rounded px-2 py-1 text-foreground focus:outline-none focus:border-primary/50 disabled:opacity-60 min-h-[32px]"
          />
          {asOfDate && (
            <span className="px-1.5 py-0.5 rounded text-[10px] border border-amber-800/50 bg-amber-900/20 text-amber-300">
              {t("discussion.backtest_badge")}
            </span>
          )}
        </div>

        <div className="flex items-center gap-2 pt-1 border-t border-border/40">
          <button
            type="button"
            onClick={saveAsDefaults}
            disabled={isStreaming}
            className="px-2 py-1 text-[11px] rounded border border-border text-muted-foreground hover:border-primary/40 hover:text-foreground transition-colors disabled:opacity-50 min-h-[28px]"
            title={t("discussion.save_as_defaults_hint")}
          >
            {t("discussion.save_as_defaults")}
          </button>
          <span className="text-[10px] text-muted-foreground">
            {t("discussion.save_as_defaults_hint")}
          </span>
        </div>
      </>
    );
  }

  function renderActions(opts: { compact?: boolean } = {}) {
    const { compact = false } = opts;
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
                className="px-3 py-1.5 rounded-md bg-red-900/30 border border-red-800 text-red-400 text-xs hover:bg-red-900/50 transition-colors min-h-[36px]"
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
              className="px-3 py-1.5 rounded-md border border-amber-800/50 text-amber-300 text-xs hover:bg-amber-900/20 transition-colors disabled:opacity-50 min-h-[36px]"
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
                  className="text-red-400 focus:text-red-300"
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

  function renderInjectForm() {
    return (
      <div className="border border-border rounded-md p-2 bg-card/40 space-y-1.5">
        <label className="text-[11px] text-muted-foreground">
          {t("discussion.inject_label")}
        </label>
        <textarea
          value={injectDraft}
          onChange={(e) => setInjectDraft(e.target.value)}
          onKeyDown={(e) => {
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
            onClick={() => {
              const trimmed = injectDraft.trim();
              if (trimmed && !injectMut.isPending) {
                injectMut.mutate(trimmed);
                setInjectSheetOpen(false);
              }
            }}
            disabled={!injectDraft.trim() || injectMut.isPending}
            className="px-2.5 py-1 rounded text-[11px] border border-amber-800/50 text-amber-300 hover:bg-amber-900/20 transition-colors disabled:opacity-40 min-h-[32px]"
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
    );
  }

  // Latest round number — used to default-expand the most recent
  // RoundSection so newcomers see content instead of a wall of
  // collapsed headers.
  const latestRound = transcript.length
    ? Math.max(...transcript.map((tn) => tn.round))
    : 0;

  // Computed "active session display title" for the mobile header.
  const activeTitle = (() => {
    if (!selectedId) return t("discussion.new");
    if (!detail) return t("common.loading");
    const tt = formatDiscussionTitle(detail as unknown as Discussion);
    if (tt.text) return tt.text;
    return tt.date ?? t("discussion.title");
  })();

  // ── render ────────────────────────────────────────────────────

  return (
    <div className="h-[calc(100vh-2.5rem)] bg-background flex overflow-hidden">
      {/* Desktop sidebar — always visible at lg+. PR-E redesign:
          tools (AutoRun / Strategy / Sweep / + 新討論) moved to
          the horizontal toolbar at the top of the main column.
          The rail now holds ONLY sessions navigation: filter chips
          on top + virtualized list below. Mobile keeps the merged
          tools-plus-sessions drawer via the Sheet path. */}
      <aside className="hidden lg:flex lg:w-60 border-r border-border flex-col shrink-0">
        <div className="p-3 shrink-0 border-b border-border">
          {renderSessionsFilter()}
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

      {/* Mobile sessions Sheet — left-side drawer triggered by the
          📁 button in the mobile header. */}
      <Sheet open={sessionsSheetOpen} onOpenChange={setSessionsSheetOpen}>
        <SheetContent side="left" className="w-80 max-w-[90vw] overflow-y-auto p-3 flex flex-col gap-3">
          <SheetHeader className="mb-1">
            <SheetTitle>{t("discussion.sessions_drawer_title")}</SheetTitle>
            <SheetDescription>{t("discussion.sessions_drawer_hint")}</SheetDescription>
          </SheetHeader>
          {renderSidebarContent()}
        </SheetContent>
      </Sheet>

      {/* Mobile config Sheet — right-side drawer triggered by the
          ⚙ button in the mobile header. Carries the same Topic /
          Rules / Personas / Market form as the desktop inline panel
          plus an inject form when applicable. Run / Conclude / etc.
          live in the sticky bottom action bar so users can fire
          them without opening the drawer. */}
      <Sheet open={configSheetOpen} onOpenChange={setConfigSheetOpen}>
        <SheetContent side="right" className="w-96 max-w-[95vw] overflow-y-auto p-4 space-y-3">
          <SheetHeader>
            <SheetTitle>{t("discussion.config_drawer_title")}</SheetTitle>
            <SheetDescription>{t("discussion.config_drawer_hint")}</SheetDescription>
          </SheetHeader>
          {renderConfigForm()}
        </SheetContent>
      </Sheet>

      {/* Mobile inject Sheet — small bottom drawer triggered from
          the More menu so users can drop a between-rounds message
          without scrolling the config drawer. */}
      <Sheet open={injectSheetOpen} onOpenChange={setInjectSheetOpen}>
        <SheetContent side="bottom" className="max-h-[60vh] overflow-y-auto p-4 space-y-3">
          <SheetHeader>
            <SheetTitle>{t("discussion.inject_label")}</SheetTitle>
          </SheetHeader>
          {renderInjectForm()}
        </SheetContent>
      </Sheet>

      {/* Main column. */}
      <div className="flex-1 flex flex-col min-w-0 min-h-0">
        {/* Mobile header — only visible on <lg. Carries the sessions
            trigger, current discussion title, settings trigger. */}
        <header className="lg:hidden border-b border-border px-3 py-2 flex items-center gap-2 shrink-0">
          <button
            type="button"
            onClick={() => setSessionsSheetOpen(true)}
            aria-label={t("discussion.sessions_drawer_title")}
            className="p-1.5 rounded text-muted-foreground hover:text-foreground hover:bg-accent/10 min-h-[36px] min-w-[36px] inline-flex items-center justify-center"
          >
            <Folder className="h-4 w-4" aria-hidden="true" />
          </button>
          <div className="flex-1 min-w-0">
            <p className="text-sm font-medium text-foreground truncate">{activeTitle}</p>
            {detail && (
              <p className="text-[10px] text-muted-foreground flex items-center gap-1.5">
                <DiscussionStatusBadge status={detail.status} />
                <span>R{detail.current_round}</span>
                {detail.as_of_date && <span>· 回測 {detail.as_of_date}</span>}
              </p>
            )}
          </div>
          <button
            type="button"
            onClick={() => setConfigSheetOpen(true)}
            aria-label={t("discussion.config_drawer_title")}
            className="p-1.5 rounded text-muted-foreground hover:text-foreground hover:bg-accent/10 min-h-[36px] min-w-[36px] inline-flex items-center justify-center"
          >
            <SettingsIcon className="h-4 w-4" aria-hidden="true" />
          </button>
        </header>

        {/* Desktop sticky action bar (PR-D). Mirrors the mobile
            bottom bar so Run / Conclude / "More" stay one click
            away regardless of how far the user scrolls the
            transcript or the config form below. streamError sits
            above the bar so rate limits stay visible without
            opening anything. */}
        <div className="hidden lg:block sticky top-0 z-10 border-b border-border bg-card/95 backdrop-blur px-4 py-2 shrink-0">
          {streamError && (
            <p className="text-xs text-red-400 bg-red-950/30 border border-red-900/50 rounded px-2 py-1 mb-2">
              {streamError}
            </p>
          )}
          {renderActions()}
        </div>

        {/* Desktop tool toolbar (PR-E) — horizontal row of
            「+ 新討論」 + 自動討論 / 策略模板 / 多日回測 popovers.
            Replaces the four cards that previously stacked in the
            240px sidebar. Only visible at lg+; mobile uses the
            Sessions Sheet which keeps the cards inline. */}
        <DiscussionToolbar
          agents={agents}
          personaName={personaName}
          onNewDiscussion={handleNewDiscussion}
          topic={topic}
          rules={rules}
          market={market}
          personaIds={personaIds}
        />

        {/* Desktop collapsible config bar (PR-D). Replaces the old
            top-pinned `max-h-[60vh]` panel that ate vertical space
            even when the user was just reading the transcript.
            Default-open the first time so newcomers see the form;
            subsequent visits respect the user's last choice via
            useCollapsible's localStorage memory. */}
        <div className="hidden lg:block border-b border-border shrink-0">
          <button
            type="button"
            onClick={desktopConfig.toggle}
            aria-expanded={desktopConfig.open}
            className="w-full flex items-center gap-2 px-4 py-2 text-left hover:bg-accent/5 transition-colors"
          >
            {desktopConfig.open ? (
              <ChevronDown className="h-3.5 w-3.5 text-muted-foreground shrink-0" aria-hidden="true" />
            ) : (
              <ChevronRight className="h-3.5 w-3.5 text-muted-foreground shrink-0" aria-hidden="true" />
            )}
            <SettingsIcon className="h-3.5 w-3.5 text-muted-foreground shrink-0" aria-hidden="true" />
            <span className="text-xs font-medium text-foreground">
              {t("discussion.config_drawer_title")}
            </span>
            {!desktopConfig.open && topic && (
              <span className="text-xs text-muted-foreground truncate ml-2 flex-1 min-w-0">
                · {topic}
              </span>
            )}
            {(topicDirty || rulesDirty) && (
              <span className="text-[10px] text-amber-400 ml-auto shrink-0">
                {t("discussion.unsaved")}
              </span>
            )}
          </button>
          {desktopConfig.open && (
            <div className="px-4 pb-3 space-y-3 max-h-[55vh] overflow-y-auto">
              {renderConfigForm()}
              {selectedId && isDraft && (detail?.current_round ?? 0) >= 1 && !isStreaming && (
                renderInjectForm()
              )}
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
                personaInitial={personaShort}
                defaultExpanded={rn === latestRound}
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

        {/* Mobile-only sticky action bar — hosts the most-used CTAs
            (Save / Run / Conclude / More) so users don't have to
            open the config drawer for the common path. honours iOS
            home-bar safe-area inset. The streamError above the bar
            keeps any rate-limit / round failure visible without
            opening the drawer. */}
        <div
          className="lg:hidden border-t border-border bg-card/95 backdrop-blur p-3 shrink-0"
          style={{ paddingBottom: "calc(env(safe-area-inset-bottom) + 0.75rem)" }}
        >
          {streamError && (
            <p className="text-[11px] text-red-400 bg-red-950/30 border border-red-900/50 rounded px-2 py-1 mb-2">
              {streamError}
            </p>
          )}
          {renderActions({ compact: true })}
        </div>
      </div>
    </div>
  );
}
