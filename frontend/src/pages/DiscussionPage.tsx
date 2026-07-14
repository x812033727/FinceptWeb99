import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { useQuery } from "@tanstack/react-query";
import {
  ChevronDown,
  ChevronRight,
  Settings as SettingsIcon,
} from "lucide-react";
import { useToastStore } from "@/store/toastStore";
import { useCollapsible as useCollapsibleHook } from "@/hooks/useCollapsible";
import type {
  Discussion,
  DiscussionDetail,
  DiscussionMarket,
  Turn,
} from "@/types/discussion";
import { DiscussionConfigForm } from "@/components/discussion/DiscussionConfigForm";
import { DiscussionToolbar } from "@/components/discussion/DiscussionToolbar";
import { InjectForm } from "@/components/discussion/InjectForm";
import {
  SessionsDrawerContent,
  SessionsRail,
} from "@/components/discussion/SessionsNav";
import { TranscriptPane } from "@/components/discussion/TranscriptPane";
import { useDiscussionMutations } from "@/components/discussion/_useDiscussionMutations";
import { useRoundStream } from "@/components/discussion/_useRoundStream";
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
} from "@/components/ui/sheet";
import {
  fetchAgents,
  fetchSession,
  fetchSessions,
  fetchRoundUsage,
  fetchRoundUsageDetail,
  formatDiscussionTitle,
  readCollapse,
  readDefaultMarket,
  readDefaultPersonas,
  readDefaultRules,
  readDefaultTopic,
  rememberCollapse,
  rememberDiscussionDefaults,
  usePersonaName,
  usePersonaShort,
} from "@/components/discussion/_helpers";
import type { CollapseState } from "@/components/discussion/_helpers";
// Presentational sub-components — pure display JSX sections extracted
// verbatim from this page (R7/G8 巨石頁拆分). All state / hooks stay here.
import { ConfigSheet } from "./DiscussionPage/ConfigSheet";
import { DesktopActionBar } from "./DiscussionPage/DesktopActionBar";
import { InjectSheet } from "./DiscussionPage/InjectSheet";
import { MobileActionBar } from "./DiscussionPage/MobileActionBar";
import { MobileHeader } from "./DiscussionPage/MobileHeader";

export default function DiscussionPage() {
  const { t } = useTranslation();
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

  // Round-streaming engine — all SSE state + runOneRound / runRounds /
  // cancel / stop, extracted verbatim into `_useRoundStream` (PR-8
  // 巨石頁拆分).
  const {
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
  } = useRoundStream({ selectedId, personaIds });

  const bottomRef = useRef<HTMLDivElement>(null);

  const { data: detail } = useQuery<DiscussionDetail>({
    queryKey: ["discussion-session", selectedId],
    queryFn: () => fetchSession(selectedId!),
    enabled: !!selectedId,
  });

  // Persisted per-round token tally for the selected discussion, so a
  // reopened discussion shows each round's "fed to AI" tokens. Live
  // `round_end` events (liveRoundUsage) take precedence over this.
  const { data: persistedRoundUsage = [] } = useQuery({
    queryKey: ["discussion-round-usage", selectedId],
    queryFn: () => fetchRoundUsage(selectedId!),
    enabled: !!selectedId,
  });

  // Persisted finer per-round usage (per persona + prompt-composition
  // breakdown) for the "ctx 用量明細" panel. Live `turn_end` rows take
  // precedence for the in-progress round; this seeds reopened
  // discussions and back-fills exact cost after a round completes.
  const { data: persistedRoundUsageDetail = [] } = useQuery({
    queryKey: ["discussion-round-usage-detail", selectedId],
    queryFn: () => fetchRoundUsageDetail(selectedId!),
    enabled: !!selectedId,
  });

  const personaName = usePersonaName(agents);
  const personaShort = usePersonaShort();

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
  // Create / update / delete / conclude / post-mortem / inject — all
  // extracted verbatim into `_useDiscussionMutations` (PR-8 巨石頁拆分).

  const {
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
  } = useDiscussionMutations({
    selectedId,
    setSelectedId,
    isStreaming,
    setStreamError,
    runOneRound,
  });

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

  // ── form actions ──────────────────────────────────────────────

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

  const status = detail?.status ?? "draft";
  const isDraft = !selectedId || status === "draft";

  // B4: which flavour of the inject form applies right now.
  //   running  → mid-round interject (queued, answered at the next
  //              turn boundary over SSE)
  //   followup → post-conclusion 追問 (one bounded synchronous turn)
  //   between  → classic between-rounds inject (PR #211)
  const injectMode: "between" | "running" | "followup" =
    isStreaming || status === "running"
      ? "running"
      : status === "done" && detail?.conclusion
        ? "followup"
        : "between";

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
  }, [setStreamingTurns, setStreamError]);

  // Computed "active session display title" for the mobile header.
  const activeTitle = (() => {
    if (!selectedId) return t("discussion.new");
    if (!detail) return t("common.loading");
    const tt = formatDiscussionTitle(detail as unknown as Discussion);
    if (tt.text) return tt.text;
    return tt.date ?? t("discussion.title");
  })();

  // ── extracted-component prop bundles (PR-8 巨石頁拆分) ────────
  // The config form and the actions bar each render TWICE (desktop
  // inline + mobile Sheet / bottom bar) and must share state, so the
  // props are bundled once here and spread at both call sites.

  const configFormProps = {
    collapse, toggleCollapse, topic, setTopic, rules, setRules,
    topicDirty, rulesDirty, saveTopic, saveRules, agents, personaIds,
    togglePersona, personaName, market, setMarket, asOfDate, setAsOfDate,
    selectedId, isDraft, isStreaming, updateMut, saveAsDefaults,
  };

  const actionsBarProps = {
    selectedId, isDraft, isStreaming, detail, newDiscussion, createMut,
    saveEdits, updateMut, stopStreaming, roundsPerClick, setRoundsPerClick,
    runRounds, loopProgress, cancelLoop, cancelling, concludeMut,
    setInjectSheetOpen, runPostMortemFlow, postMortemMut, personaName,
    deleteMut,
  };

  const injectFormProps = {
    injectDraft, setInjectDraft, injectMut, setInjectSheetOpen,
    // B4: interject wiring — persona roster of the ACTIVE discussion
    // (not the unsaved form selection) so the target select matches
    // what the backend will validate against.
    interjectMut, interjectTarget, setInterjectTarget,
    personaIds: detail?.persona_ids ?? [], personaName,
  };

  const sessionsFilterProps = {
    sessions, filteredSessions, selectedId, setSelectedId,
    sessionsQuery, setSessionsQuery, sessionsStatusFilter, setSessionsStatusFilter,
  };

  // ── render ────────────────────────────────────────────────────

  return (
    <div className="h-[calc(100vh-2.75rem)] bg-background flex overflow-hidden">
      {/* Desktop sidebar — sessions filter + virtualized list. */}
      <SessionsRail {...sessionsFilterProps} />

      {/* Mobile sessions Sheet — left-side drawer triggered by the
          📁 button in the mobile header. */}
      <Sheet open={sessionsSheetOpen} onOpenChange={setSessionsSheetOpen}>
        <SheetContent side="left" className="w-80 max-w-[90vw] overflow-y-auto p-3 flex flex-col gap-3">
          <SheetHeader className="mb-1">
            <SheetTitle>{t("discussion.sessions_drawer_title")}</SheetTitle>
            <SheetDescription>{t("discussion.sessions_drawer_hint")}</SheetDescription>
          </SheetHeader>
          <SessionsDrawerContent
            {...sessionsFilterProps}
            agents={agents}
            collapse={collapse}
            toggleCollapse={toggleCollapse}
            personaName={personaName}
            topic={topic}
            rules={rules}
            market={market}
            personaIds={personaIds}
            handleNewDiscussion={handleNewDiscussion}
            setSessionsSheetOpen={setSessionsSheetOpen}
          />
        </SheetContent>
      </Sheet>

      {/* Mobile config Sheet — right-side drawer triggered by the
          ⚙ button in the mobile header. Carries the same Topic /
          Rules / Personas / Market form as the desktop inline panel
          plus an inject form when applicable. Run / Conclude / etc.
          live in the sticky bottom action bar so users can fire
          them without opening the drawer. */}
      <ConfigSheet
        configSheetOpen={configSheetOpen}
        setConfigSheetOpen={setConfigSheetOpen}
        t={t}
        configFormProps={configFormProps}
      />

      {/* Mobile inject Sheet — small bottom drawer triggered from
          the More menu so users can drop a between-rounds message
          without scrolling the config drawer. */}
      <InjectSheet
        injectSheetOpen={injectSheetOpen}
        setInjectSheetOpen={setInjectSheetOpen}
        t={t}
        injectMode={injectMode}
        injectFormProps={injectFormProps}
      />

      {/* Main column. */}
      <div className="flex-1 flex flex-col min-w-0 min-h-0">
        {/* Mobile header — only visible on <lg. Carries the sessions
            trigger, current discussion title, settings trigger. */}
        <MobileHeader
          t={t}
          setSessionsSheetOpen={setSessionsSheetOpen}
          activeTitle={activeTitle}
          detail={detail}
          setConfigSheetOpen={setConfigSheetOpen}
        />

        {/* Desktop sticky action bar (PR-D). Mirrors the mobile
            bottom bar so Run / Conclude / "More" stay one click
            away regardless of how far the user scrolls the
            transcript or the config form below. streamError sits
            above the bar so rate limits stay visible without
            opening anything. */}
        <DesktopActionBar streamError={streamError} actionsBarProps={actionsBarProps} />

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
              <span className="text-micro text-warning ml-auto shrink-0">
                {t("discussion.unsaved")}
              </span>
            )}
          </button>
          {desktopConfig.open && (
            <div className="px-4 pb-3 space-y-3 max-h-[55vh] overflow-y-auto">
              <DiscussionConfigForm {...configFormProps} />
              {selectedId && isDraft && (detail?.current_round ?? 0) >= 1 && !isStreaming && (
                <InjectForm {...injectFormProps} />
              )}
            </div>
          )}
        </div>

        {/* transcript */}
        <TranscriptPane
          selectedId={selectedId}
          detail={detail}
          transcript={transcript}
          personaName={personaName}
          personaShort={personaShort}
          isStreaming={isStreaming}
          streamingPersona={streamingPersona}
          streamingRound={streamingRound}
          streamingStage={streamingStage}
          streamingProgress={streamingProgress}
          streamBuffer={streamBuffer}
          streamingToolEvents={streamingToolEvents}
          persistedRoundUsage={persistedRoundUsage}
          persistedRoundUsageDetail={persistedRoundUsageDetail}
          liveRoundUsage={liveRoundUsage}
          liveRoundUsageDetail={liveRoundUsageDetail}
          postMortemMut={postMortemMut}
          persistedPostMortem={persistedPostMortem}
          bottomRef={bottomRef}
          injectMode={injectMode}
          injectFormProps={injectFormProps}
        />

        {/* Mobile-only sticky action bar — hosts the most-used CTAs
            (Save / Run / Conclude / More) so users don't have to
            open the config drawer for the common path. honours iOS
            home-bar safe-area inset. The streamError above the bar
            keeps any rate-limit / round failure visible without
            opening the drawer. */}
        <MobileActionBar streamError={streamError} actionsBarProps={actionsBarProps} />
      </div>
    </div>
  );
}
