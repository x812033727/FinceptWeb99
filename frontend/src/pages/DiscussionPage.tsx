import { useEffect, useMemo, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useNavigate } from "react-router-dom";
import api, { notifyRateLimited } from "@/lib/api";
import { useAuthStore } from "@/store/authStore";

// ── types ──────────────────────────────────────────────────────────

interface AgentInfo {
  id: string;
  name: string;
  description: string;
  default_provider: string;
}

interface Turn {
  id: number;
  round: number;
  turn_index: number;
  persona_id: string;
  stance: "agree" | "dissent" | "supplement";
  content: string;
  created_at: string;
}

interface Conclusion {
  recommended_symbols: string[];
  reasoning: string;
  risks: string[];
  time_horizon: "short_term" | "medium_term" | "long_term";
  consensus_score: number;
  _parse_error?: boolean;
}

interface Discussion {
  id: string;
  topic: string;
  rules: string;
  persona_ids: string[];
  status: "draft" | "running" | "done";
  current_round: number;
  conclusion: Conclusion | null;
  verdict?: "win" | "loss" | "unverifiable" | null;
  verdict_reason?: string | null;
  verified_at?: string | null;
  auto_run?: boolean;
  day1_open_prices?: Record<string, number> | null;
  day5_close_prices?: Record<string, number> | null;
  created_at: string;
  updated_at: string;
}

interface DiscussionDetail extends Discussion {
  turns: Turn[];
}

// ── defaults ──────────────────────────────────────────────────────

const DEFAULT_TOPIC =
  "找出本週（未來 5 個交易日）值得短線進場的台股 1-3 檔，並列出進場條件與停損點。";

const DEFAULT_RULES = [
  "1. 每位專家發言 ≤ 200 字。",
  "2. 必須引用至少一個具體數據（價、量、法人、新聞情緒）。",
  "3. 反對其他專家時必須點名是反對誰、為什麼。",
  "4. 不得推薦未在「市場現況」的 top_gainers / top_losers 中出現的標的。",
  "5. 短線定義：5 個交易日內出場。",
].join("\n");

// localStorage keys for "use my last-saved values as the defaults for
// the next new discussion". Per-browser; clears with site data.
const LS_TOPIC_KEY = "fincept.discussion.last_topic";
const LS_RULES_KEY = "fincept.discussion.last_rules";

function readDefaultTopic(): string {
  try {
    return localStorage.getItem(LS_TOPIC_KEY) ?? DEFAULT_TOPIC;
  } catch {
    return DEFAULT_TOPIC;
  }
}
function readDefaultRules(): string {
  try {
    return localStorage.getItem(LS_RULES_KEY) ?? DEFAULT_RULES;
  } catch {
    return DEFAULT_RULES;
  }
}
function rememberTopic(topic: string): void {
  try {
    localStorage.setItem(LS_TOPIC_KEY, topic);
  } catch {
    /* localStorage disabled (private mode, full quota) — silent */
  }
}
function rememberRules(rules: string): void {
  try {
    localStorage.setItem(LS_RULES_KEY, rules);
  } catch {
    /* see rememberTopic */
  }
}

// Per-section collapse state. Stored as a single JSON blob so adding a
// new collapsible doesn't churn another LS key. Falls back to "everything
// expanded" if storage is disabled / corrupt.
const LS_COLLAPSE_KEY = "fincept.discussion.collapse";

interface CollapseState {
  topic: boolean;
  rules: boolean;
  personas: boolean;
  sidebar: boolean;
}

const DEFAULT_COLLAPSE: CollapseState = {
  topic: false,
  rules: false,
  personas: false,
  sidebar: false,
};

function readCollapse(): CollapseState {
  try {
    const raw = localStorage.getItem(LS_COLLAPSE_KEY);
    if (raw) return { ...DEFAULT_COLLAPSE, ...JSON.parse(raw) };
  } catch {
    // ignore — fall through to mobile-aware default
  }
  // First-time visitors on a phone have ~600px of vertical space — the
  // sidebar + topic + rules + personas all expanded would push the
  // transcript off-screen. Default the heaviest sections closed on
  // mobile so the discussion content is visible immediately. Tablet /
  // desktop (≥ 1024px) keep the original "everything open" default.
  const isMobile =
    typeof window !== "undefined" && window.innerWidth < 1024;
  return isMobile
    ? { ...DEFAULT_COLLAPSE, sidebar: true, personas: true, rules: true }
    : DEFAULT_COLLAPSE;
}

function rememberCollapse(s: CollapseState): void {
  try {
    localStorage.setItem(LS_COLLAPSE_KEY, JSON.stringify(s));
  } catch {
    /* private mode / quota — silent */
  }
}

const DEFAULT_PERSONAS = ["market_analyst", "trading_coach", "lynch", "simons"];

const STANCE_BADGE: Record<Turn["stance"], { label: string; cls: string }> = {
  agree: { label: "✓ 同意", cls: "bg-green-900/30 text-green-300 border-green-800/50" },
  dissent: { label: "✗ 異議", cls: "bg-red-900/30 text-red-300 border-red-800/50" },
  supplement: { label: "↳ 補充", cls: "bg-blue-900/30 text-blue-300 border-blue-800/50" },
};

// ── api helpers ────────────────────────────────────────────────────

async function fetchAgents(): Promise<AgentInfo[]> {
  const res = await api.get<AgentInfo[]>("/ai/agents");
  return res.data;
}

async function fetchSessions(): Promise<Discussion[]> {
  const res = await api.get<Discussion[]>("/discussion/sessions");
  return res.data;
}

async function fetchSession(id: string): Promise<DiscussionDetail> {
  const res = await api.get<DiscussionDetail>(`/discussion/sessions/${id}`);
  return res.data;
}

async function createSession(body: {
  topic: string;
  rules: string;
  persona_ids: string[];
}): Promise<Discussion> {
  const res = await api.post<Discussion>("/discussion/sessions", body);
  return res.data;
}

async function updateSession(
  id: string,
  body: { topic?: string; rules?: string; persona_ids?: string[] },
): Promise<Discussion> {
  const res = await api.patch<Discussion>(`/discussion/sessions/${id}`, body);
  return res.data;
}

async function deleteSession(id: string): Promise<void> {
  await api.delete(`/discussion/sessions/${id}`);
}

async function concludeSession(id: string): Promise<{ conclusion: Conclusion }> {
  const res = await api.post<{ conclusion: Conclusion }>(
    `/discussion/sessions/${id}/conclude`,
  );
  return res.data;
}

// ── persona-name lookup ────────────────────────────────────────────

function usePersonaName(agents: AgentInfo[]) {
  const { t, i18n } = useTranslation();
  return (id: string) => {
    const a = agents.find((x) => x.id === id);
    if (!a) return id;
    const key = `personas.agents.${id}.name`;
    return i18n.exists(key) ? t(key) : a.name;
  };
}

// ── date formatting ────────────────────────────────────────────────
// Discussions are snapshots of a market view. Showing when each was
// captured matters more than the exact time — a re-evaluation a week
// later may reach different conclusions even on the same topic.

function formatDateShort(iso: string | undefined): string {
  if (!iso) return "";
  const d = new Date(iso);
  if (isNaN(d.getTime())) return "";
  return d.toLocaleDateString(undefined, { month: "numeric", day: "numeric" });
}

function formatDateLong(iso: string | undefined): string {
  if (!iso) return "";
  const d = new Date(iso);
  if (isNaN(d.getTime())) return "";
  return d.toLocaleString(undefined, {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  });
}

// ── dynamic discussion title ──────────────────────────────────────
// Once a discussion has been concluded (recommended_symbols populated)
// the sidebar/title stops showing the user-typed topic and renders
// `YYYYMMDD(sym1,sym2,sym3)` instead — that's what makes the auto-run
// feed feel like a track record. The verdict suffix (勝/敗) lands
// after the verifier task grades the row 5 trading days later.
//
// Date is formatted in Asia/Taipei because the discussion's "day 1"
// is the TW trading day, not the browser-local one. A discussion
// created at 23:00 UTC (07:00 next day in Taipei) belongs to that
// next day's track record.

function _formatTaipeiDateCompact(iso: string): string {
  const d = new Date(iso);
  if (isNaN(d.getTime())) return "";
  // en-CA gives YYYY-MM-DD; strip dashes for the compact YYYYMMDD form.
  const parts = new Intl.DateTimeFormat("en-CA", {
    timeZone: "Asia/Taipei",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  }).format(d);
  return parts.replace(/-/g, "");
}

interface FormattedSymbolLine {
  symbol: string;
  open: number | null;
  close: number | null;
  gainPct: number | null;
  cls: string;
}

interface FormattedTitle {
  // Single fallback line when there's no conclusion yet.
  text?: string;
  // Otherwise structured multi-line layout.
  date?: string;
  verdictMark?: string;       // "" / "勝" / "敗"
  verdictCls?: string;        // text-green-500 etc.
  lines?: FormattedSymbolLine[];
}

function _toFixedSmart(n: number): string {
  // Show prices with up to 2 decimals, trimming trailing zeros so
  // round-number TWSE prices read "55" not "55.00".
  return Number.isInteger(n) ? n.toString() : (Math.round(n * 100) / 100).toString();
}

function _signedPct(n: number): string {
  const sign = n >= 0 ? "+" : "";
  return `${sign}${(n * 100).toFixed(1)}%`;
}

function formatDiscussionTitle(s: {
  topic: string;
  conclusion: Conclusion | null;
  verdict?: "win" | "loss" | "unverifiable" | null;
  created_at: string;
  day1_open_prices?: Record<string, number> | null;
  day5_close_prices?: Record<string, number> | null;
}): FormattedTitle {
  const syms = s.conclusion?.recommended_symbols ?? [];
  if (!syms.length) {
    // No conclusion yet (or synthesizer returned empty) — fall back
    // to the user-typed topic so the row stays informative.
    return { text: s.topic };
  }
  const date = _formatTaipeiDateCompact(s.created_at);

  let verdictMark = "";
  let verdictCls = "text-foreground";
  if (s.verdict === "win") { verdictMark = "勝"; verdictCls = "text-green-500"; }
  else if (s.verdict === "loss") { verdictMark = "敗"; verdictCls = "text-red-500"; }
  else if (s.verdict === "unverifiable") { verdictCls = "text-muted-foreground"; }

  const opens = s.day1_open_prices ?? {};
  const closes = s.day5_close_prices ?? {};
  const lines: FormattedSymbolLine[] = syms.slice(0, 3).map((sym) => {
    const open = sym in opens ? opens[sym] : null;
    const close = sym in closes ? closes[sym] : null;
    const gainPct =
      open !== null && open > 0 && close !== null
        ? (close - open) / open
        : null;
    let cls = "text-muted-foreground";
    if (gainPct !== null) {
      cls = gainPct >= 0 ? "text-green-500" : "text-red-500";
    }
    return { symbol: sym, open, close, gainPct, cls };
  });

  return { date, verdictMark, verdictCls, lines };
}

// ── inline markdown renderer ──────────────────────────────────────
// Personas are prompted to use `**...**` for key points (target prices,
// stock codes, conclusions). We do a minimal split-and-render here
// instead of pulling in react-markdown — bold is the only feature in
// play, the input is already trusted (LLM-generated, server-side
// parsed), and a 30-line tokenizer beats a ~50KB dependency for the
// scope. Percentages and signed numbers are colored automatically as
// a bonus so "+5.2%" / "-3.1%" pop visually.

const _BOLD_RE = /\*\*([^*\n][^*\n]*?)\*\*/g;
const _SIGNED_PCT_RE = /([+-]?\d+(?:\.\d+)?%)/g;

function colorizeNumbers(text: string, baseKey: string): React.ReactNode[] {
  // Returns an array of nodes where signed-percent tokens are wrapped
  // in a colored span (green for +, red for -). Plain numbers are
  // left alone — too noisy to color every digit.
  const parts = text.split(_SIGNED_PCT_RE);
  return parts.map((part, idx) => {
    if (idx % 2 === 1) {
      const positive = part.startsWith("+");
      const negative = part.startsWith("-");
      const cls = positive
        ? "text-emerald-400"
        : negative
          ? "text-red-400"
          : "";
      return cls ? (
        <span key={`${baseKey}-pct-${idx}`} className={cls}>
          {part}
        </span>
      ) : (
        part
      );
    }
    return part;
  });
}

function renderInlineMarkdown(text: string): React.ReactNode[] {
  // Split on `**...**` matches; even-indexed pieces are plain text,
  // odd-indexed pieces are the captured bold content. Preserves
  // surrounding whitespace + non-bold text untouched.
  const segments: React.ReactNode[] = [];
  let cursor = 0;
  let key = 0;
  for (const match of text.matchAll(_BOLD_RE)) {
    const start = match.index ?? 0;
    if (start > cursor) {
      segments.push(
        ...colorizeNumbers(text.slice(cursor, start), `t-${key++}`),
      );
    }
    segments.push(
      <strong key={`b-${key++}`} className="font-semibold text-primary">
        {colorizeNumbers(match[1], `bp-${key++}`)}
      </strong>,
    );
    cursor = start + match[0].length;
  }
  if (cursor < text.length) {
    segments.push(...colorizeNumbers(text.slice(cursor), `t-${key++}`));
  }
  return segments;
}

// ── main page ──────────────────────────────────────────────────────

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

  // Streaming round state — appended to as the SSE arrives.
  const [streamingTurns, setStreamingTurns] = useState<Turn[]>([]);
  const [streamingPersona, setStreamingPersona] = useState<string | null>(null);
  const [streamBuffer, setStreamBuffer] = useState("");
  const [streamingRound, setStreamingRound] = useState<number | null>(null);
  const [isStreaming, setIsStreaming] = useState(false);
  const [streamError, setStreamError] = useState<string | null>(null);
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
    setStreamError(null);
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
    mutationFn: (body: { topic?: string; rules?: string; persona_ids?: string[] }) =>
      updateSession(selectedId!, body),
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
    createMut.mutate({ topic, rules, persona_ids: personaIds });
  }

  function saveEdits() {
    if (!selectedId) return;
    updateMut.mutate({ topic, rules, persona_ids: personaIds });
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
              case "turn_start":
                currentBuffer = "";
                setStreamingPersona(obj.persona_id);
                if (typeof obj.round === "number") setStreamingRound(obj.round);
                setStreamBuffer("");
                break;
              case "delta":
                currentBuffer += obj.text;
                setStreamBuffer(currentBuffer);
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
        className={`lg:w-80 border-b lg:border-b-0 lg:border-r border-border flex-col p-3 lg:p-4 gap-3 shrink-0 overflow-y-auto max-h-[28vh] lg:max-h-none ${
          collapse.sidebar ? "hidden" : "flex"
        }`}
      >
        <div>
          <h2 className="text-sm font-semibold text-foreground">{t("discussion.title")}</h2>
          <p className="text-xs text-muted-foreground mt-0.5">{t("discussion.subtitle")}</p>
        </div>

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
                        {ln.symbol}:
                        {ln.open !== null ? _toFixedSmart(ln.open) : "—"}
                        /
                        {ln.close !== null ? _toFixedSmart(ln.close) : "—"}
                        {" "}
                        ({ln.gainPct !== null ? _signedPct(ln.gainPct) : "—"})
                      </div>
                    ))}
                  </div>
                );
              })()}
              <div className="mt-0.5 flex items-center gap-2 text-[10px]">
                <span>{formatDateShort(s.updated_at || s.created_at)}</span>
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
          {transcript.map((tn, i) => {
            const badge = STANCE_BADGE[tn.stance] ?? STANCE_BADGE.supplement;
            const body =
              tn.stance === "agree" && !tn.content.trim()
                ? t("discussion.agree_silent")
                : tn.content;
            const prevRound = i > 0 ? transcript[i - 1].round : null;
            const showRoundHeader = prevRound !== tn.round;
            return (
              <div key={`${tn.round}-${tn.turn_index}-${i}`}>
                {showRoundHeader && (
                  <div className="flex items-center gap-2 my-3 first:mt-0">
                    <span className="text-[11px] font-semibold text-primary tracking-wider">
                      {t("discussion.round_label", { round: tn.round })}
                    </span>
                    <span className="flex-1 h-px bg-border" />
                  </div>
                )}
                <div className="bg-card border border-border rounded-lg p-3">
                  <div className="flex items-center gap-2 text-xs text-muted-foreground mb-1">
                    <span className="font-bold text-red-500">
                      {personaName(tn.persona_id)}
                    </span>
                    <span>·</span>
                    <span>R{tn.round}</span>
                    <span>·</span>
                    <span className={`px-1.5 py-0.5 rounded border text-[10px] ${badge.cls}`}>
                      {badge.label}
                    </span>
                  </div>
                  <div className="text-sm text-foreground whitespace-pre-wrap leading-relaxed">
                    {renderInlineMarkdown(body)}
                  </div>
                </div>
              </div>
            );
          })}
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
          <div ref={bottomRef} />
        </div>
      </div>
    </div>
  );
}


// ── conclusion card with export + handoff actions ──────────────────

function buildMarkdownExport(
  detail: DiscussionDetail,
  personaName: (id: string) => string,
): string {
  const lines: string[] = [];
  lines.push(`# ${detail.topic}`);
  lines.push("");
  lines.push("## 共同規則");
  lines.push("");
  lines.push("```");
  lines.push(detail.rules);
  lines.push("```");
  lines.push("");
  lines.push("## 出席專家");
  lines.push("");
  for (const pid of detail.persona_ids) {
    lines.push(`- ${personaName(pid)} (${pid})`);
  }
  lines.push("");

  // Group turns by round so the markdown reads chronologically.
  const turnsByRound = new Map<number, Turn[]>();
  for (const tn of detail.turns) {
    if (!turnsByRound.has(tn.round)) turnsByRound.set(tn.round, []);
    turnsByRound.get(tn.round)!.push(tn);
  }
  const sortedRounds = [...turnsByRound.keys()].sort((a, b) => a - b);
  for (const r of sortedRounds) {
    lines.push(`## 第 ${r} 輪`);
    lines.push("");
    const roundTurns = (turnsByRound.get(r) ?? []).slice().sort(
      (a, b) => a.turn_index - b.turn_index,
    );
    for (const tn of roundTurns) {
      const stanceLabel =
        tn.stance === "agree" ? "✓ 同意" :
        tn.stance === "dissent" ? "✗ 異議" : "↳ 補充";
      lines.push(`### ${personaName(tn.persona_id)} — ${stanceLabel}`);
      lines.push("");
      lines.push(tn.content.trim() || "_（同意，無補充）_");
      lines.push("");
    }
  }

  if (detail.conclusion) {
    const c = detail.conclusion;
    lines.push("## 結論");
    lines.push("");
    if (c.recommended_symbols.length) {
      lines.push(`- 推薦標的：${c.recommended_symbols.join(", ")}`);
    }
    lines.push(`- 共識度：${(c.consensus_score * 100).toFixed(0)}%`);
    lines.push(`- 時間框架：${c.time_horizon}`);
    lines.push("");
    lines.push("### 理由");
    lines.push("");
    lines.push(c.reasoning);
    if (c.risks.length) {
      lines.push("");
      lines.push("### 風險");
      lines.push("");
      for (const risk of c.risks) {
        lines.push(`- ${risk}`);
      }
    }
  }
  lines.push("");
  return lines.join("\n");
}

function ConclusionCard({
  detail,
  personaName,
}: {
  detail: DiscussionDetail;
  personaName: (id: string) => string;
}) {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const conclusion = detail.conclusion!;

  // Triggers a browser download of the rendered markdown. Filename uses
  // the topic (sanitised) + ISO date so users can recognise exports
  // among many.
  function exportMarkdown() {
    const md = buildMarkdownExport(detail, personaName);
    const blob = new Blob([md], { type: "text/markdown;charset=utf-8" });
    const url = URL.createObjectURL(blob);
    const safeTopic = detail.topic.slice(0, 40).replace(/[\\/:*?"<>|]/g, "_");
    const date = new Date().toISOString().slice(0, 10);
    const a = document.createElement("a");
    a.href = url;
    a.download = `discussion-${date}-${safeTopic}.md`;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);
  }

  // Hands off the conclusion to the AIPage for deeper one-on-one
  // analysis. Picks the first persona from the discussion's roster as
  // the seed agent — users can switch in the AIPage. The recommended
  // symbols + topic ride along as `context` so the AI sees the full
  // round-table outcome without us having to re-paste it.
  function askPersonaAbout(symbol?: string) {
    const seedPersona = detail.persona_ids[0];
    const target = symbol ?? conclusion.recommended_symbols[0] ?? "";
    const prompt = symbol
      ? t("discussion.followup_prompt_symbol", { symbol })
      : t("discussion.followup_prompt_general");
    navigate("/ai", {
      state: {
        agentId: seedPersona,
        initialMessage: prompt,
        context: {
          source: "discussion",
          topic: detail.topic,
          recommended_symbols: conclusion.recommended_symbols,
          consensus_score: conclusion.consensus_score,
          time_horizon: conclusion.time_horizon,
          focused_symbol: target || undefined,
        },
      },
    });
  }

  // When the synthesizer's output couldn't be parsed as JSON we treat
  // the conclusion as degraded — paint the card red, hide the actions
  // (export / deep-dive) so users don't accidentally rely on garbage,
  // and prompt them to re-synthesize.
  const hasError = !!conclusion._parse_error;
  const cardClass = hasError
    ? "bg-red-950/20 border border-red-800/60 rounded-lg p-4 mt-4"
    : "bg-amber-950/20 border border-amber-800/50 rounded-lg p-4 mt-4";
  const titleClass = hasError ? "text-sm font-semibold text-red-300" : "text-sm font-semibold text-amber-300";

  return (
    <div className={cardClass}>
      <div className="flex items-center justify-between mb-2 gap-2">
        <h3 className={titleClass}>
          {hasError
            ? `⚠ ${t("discussion.conclusion_title")}`
            : t("discussion.conclusion_title")}
        </h3>
        {!hasError && (
          <div className="flex gap-1.5 shrink-0">
            <button
              onClick={exportMarkdown}
              className="px-2.5 py-1 text-xs border border-border text-muted-foreground rounded hover:text-foreground"
            >
              {t("discussion.export_markdown")}
            </button>
            {conclusion.recommended_symbols.length > 0 && (
              <button
                onClick={() => askPersonaAbout()}
                className="px-2.5 py-1 text-xs border border-amber-800/50 text-amber-300 rounded hover:bg-amber-900/30"
              >
                {t("discussion.ask_followup")}
              </button>
            )}
          </div>
        )}
      </div>
      {hasError ? (
        <p className="text-xs text-red-300">{t("discussion.conclusion_parse_error")}</p>
      ) : (
        <>
          {conclusion.recommended_symbols.length > 0 && (
            <div className="mb-2">
              <span className="text-xs text-muted-foreground">
                {t("discussion.recommended_symbols")}：
              </span>
              <div className="mt-1 flex flex-wrap gap-1.5">
                {conclusion.recommended_symbols.map((s) => (
                  <button
                    key={s}
                    onClick={() => askPersonaAbout(s)}
                    title={t("discussion.click_for_deep_dive")}
                    className="px-2 py-0.5 rounded bg-amber-900/30 text-amber-200 text-xs font-mono hover:bg-amber-900/60"
                  >
                    {s}
                  </button>
                ))}
              </div>
            </div>
          )}
          <p className="text-sm text-foreground mb-2 whitespace-pre-wrap">
            {renderInlineMarkdown(conclusion.reasoning)}
          </p>
          {conclusion.risks.length > 0 && (
            <div className="mb-2">
              <span className="text-xs text-muted-foreground">
                {t("discussion.risks")}：
              </span>
              <ul className="mt-1 list-disc list-inside text-xs text-foreground/80 space-y-0.5">
                {conclusion.risks.map((r, idx) => (
                  <li key={idx}>{r}</li>
                ))}
              </ul>
            </div>
          )}
          <div className="text-xs text-muted-foreground">
            {t("discussion.consensus")}：
            <span className="font-mono ml-1">
              {(conclusion.consensus_score * 100).toFixed(0)}%
            </span>
            <span className="ml-3">
              {t("discussion.horizon")}：
              <span className="ml-1">
                {t(`discussion.horizon_${conclusion.time_horizon}`)}
              </span>
            </span>
          </div>
        </>
      )}
    </div>
  );
}
