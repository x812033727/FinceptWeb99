import { useState, useRef, useEffect } from "react";
import { useQuery } from "@tanstack/react-query";
import { useLocation } from "react-router-dom";
import { useTranslation } from "react-i18next";
import api from "@/lib/api";
import { useAuthStore } from "@/store/authStore";
import { MessageBubble } from "@/components/ai/MessageBubble";
import { ChatComposer } from "@/components/ai/ChatComposer";
import { ChatHeader } from "@/components/ai/ChatHeader";
import { PersonaList } from "@/components/ai/PersonaList";
import { useChatStream } from "@/components/ai/_useChatStream";
import { fetchAgents } from "@/components/discussion/_api";

// ── types ──────────────────────────────────────────────────────────

interface UserMe {
  ai_requests_remaining: number | null;
}

// ── api helpers ────────────────────────────────────────────────────

async function fetchMe(): Promise<UserMe> {
  const res = await api.get<UserMe>("/auth/me");
  return res.data;
}

// ── main page ──────────────────────────────────────────────────────

export default function AIPage() {
  const { t, i18n } = useTranslation();
  const role = useAuthStore((s) => s.user?.role);
  const location = useLocation();
  const navState = location.state as {
    agentId?: string;
    initialMessage?: string;
    context?: Record<string, unknown>;
  } | null;

  const { data: agents = [], isLoading } = useQuery({
    queryKey: ["ai-agents"],
    queryFn: fetchAgents,
  });

  // Quota gauge (today's remaining AI requests). Re-fetches after a
  // successful send via TanStack Query's invalidation in onSuccess
  // below, so the badge counts down in real time.
  const { data: me, refetch: refetchMe } = useQuery({
    queryKey: ["me"],
    queryFn: fetchMe,
    staleTime: 60_000,
  });

  const [selectedAgent, setSelectedAgent] = useState<string>(navState?.agentId ?? "");
  const [context] = useState<Record<string, unknown>>(navState?.context ?? {});
  const [useClaudeAgent, setUseClaudeAgent] = useState(false);
  const [personaSheetOpen, setPersonaSheetOpen] = useState(false);
  const bottomRef = useRef<HTMLDivElement>(null);
  const sentNavState = useRef(false);

  const canUseClaudeAgent = role === "analyst" || role === "admin";

  // SSE chat engine (messages / input / streaming / error state +
  // sendMessage / stopGeneration) — extracted verbatim into
  // `_useChatStream` (PR-8 巨石頁拆分).
  const {
    messages,
    setMessages,
    input,
    setInput,
    streaming,
    error,
    setError,
    sendMessage,
    stopGeneration,
  } = useChatStream({
    agents,
    selectedAgent,
    context,
    useClaudeAgent,
    canUseClaudeAgent,
    refetchMe,
    initialInput: navState?.initialMessage ?? "",
  });

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect
    if (agents.length && !selectedAgent) setSelectedAgent(agents[0].id);
  }, [agents, selectedAgent]);

  useEffect(() => {
    if (navState?.initialMessage && agents.length && selectedAgent && !sentNavState.current) {
      sentNavState.current = true;
      sendMessage();
    }
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [agents, selectedAgent]);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages]);

  function switchAgent(id: string) {
    if (streaming) return;
    setSelectedAgent(id);
    setMessages([]);
    setError(null);
    setPersonaSheetOpen(false);
  }

  function clearChat() {
    if (streaming) return;
    setMessages([]);
    setError(null);
  }

  const activeAgent = agents.find((a) => a.id === selectedAgent);
  const activeAgentName = activeAgent
    ? (i18n.exists(`personas.agents.${activeAgent.id}.name`)
        ? t(`personas.agents.${activeAgent.id}.name`)
        : activeAgent.name)
    : t("ai.header_default");
  const activeAgentDesc = activeAgent
    ? (i18n.exists(`personas.agents.${activeAgent.id}.description`)
        ? t(`personas.agents.${activeAgent.id}.description`)
        : activeAgent.description)
    : "";
  const effectiveIsClaudeAgent =
    activeAgent?.default_provider === "claude_agent" ||
    (useClaudeAgent && canUseClaudeAgent);
  const claudeAgentToggleVisible =
    canUseClaudeAgent && !!activeAgent && activeAgent.default_provider !== "claude_agent";
  const remaining = me?.ai_requests_remaining ?? null;

  return (
    // h-[calc(100vh-2.75rem)] = viewport minus AppLayout's 40px (h-10) topbar.
    // h-full doesn't always resolve reliably through the AppLayout chain
    // (flex-1 main → ErrorBoundary → AIPage), so we pin the height explicitly.
    <div className="h-[calc(100vh-2.75rem)] bg-background flex overflow-hidden">
      {/* Desktop sidebar — always visible on lg+, hidden on mobile (Sheet
          takes over). Keeps the agent list within thumb reach without
          eating mobile screen real estate. */}
      <aside className="hidden lg:flex lg:w-64 border-r border-border flex-col p-4 gap-3 shrink-0 overflow-y-auto">
        <div>
          <h2 className="text-sm font-semibold text-foreground">{t("ai.title")}</h2>
          <p className="text-xs text-muted-foreground mt-0.5">{t("ai.subtitle")}</p>
        </div>
        <PersonaList
          agents={agents}
          selectedAgent={selectedAgent}
          onPick={switchAgent}
          isLoading={isLoading}
        />
        <div className="mt-auto text-xs text-muted-foreground">
          <a href="/dashboard" className="hover:text-foreground transition-colors">{t("ai.back_dashboard")}</a>
        </div>
      </aside>

      {/* Main chat column. */}
      <div className="flex-1 flex flex-col min-w-0 min-h-0">
        <ChatHeader
          personaSheetOpen={personaSheetOpen}
          setPersonaSheetOpen={setPersonaSheetOpen}
          agents={agents}
          selectedAgent={selectedAgent}
          switchAgent={switchAgent}
          isLoading={isLoading}
          activeAgentName={activeAgentName}
          activeAgentDesc={activeAgentDesc}
          remaining={remaining}
          claudeAgentToggleVisible={claudeAgentToggleVisible}
          effectiveIsClaudeAgent={effectiveIsClaudeAgent}
          useClaudeAgent={useClaudeAgent}
          setUseClaudeAgent={setUseClaudeAgent}
          streaming={streaming}
          messages={messages}
          clearChat={clearChat}
        />

        <div className="flex-1 overflow-y-auto px-3 sm:px-6 py-4 space-y-4 min-h-0">
          {messages.length === 0 && (
            <div className="h-full flex items-center justify-center">
              <p className="text-sm text-muted-foreground text-center">
                {activeAgent
                  ? t("ai.ask_placeholder", { agent: activeAgentName })
                  : t("ai.select_to_begin")}
              </p>
            </div>
          )}
          {messages.map((msg, i) => (
            <MessageBubble key={i} msg={msg} />
          ))}
          {error && (
            <div className="text-xs text-danger bg-danger/10 border border-danger/30 rounded px-3 py-2">
              {error}
            </div>
          )}
          <div ref={bottomRef} />
        </div>

        <ChatComposer
          value={input}
          onChange={setInput}
          onSend={sendMessage}
          onStop={stopGeneration}
          streaming={streaming}
          disabled={!selectedAgent}
          placeholder={t("ai.input_placeholder")}
          disclaimerText={t("ai.disclaimer")}
        />
      </div>
    </div>
  );
}
