import { useState, useRef, useEffect } from "react";
import { useQuery } from "@tanstack/react-query";
import { useLocation } from "react-router-dom";
import { useTranslation } from "react-i18next";
import api, { notifyRateLimited } from "@/lib/api";
import { useAuthStore } from "@/store/authStore";

// ── types ──────────────────────────────────────────────────────────

interface AgentInfo {
  id: string;
  name: string;
  description: string;
  default_provider: string;
}

interface ToolCallEvent {
  id: string;
  name: string;
  args: unknown;
  result?: string;
  isError?: boolean;
  status: "running" | "done" | "error";
}

interface ChatMessage {
  role: "user" | "assistant";
  content: string;
  streaming?: boolean;
  toolCalls?: ToolCallEvent[];
}

// ── api helpers ────────────────────────────────────────────────────

async function fetchAgents(): Promise<AgentInfo[]> {
  const res = await api.get<AgentInfo[]>("/ai/agents");
  return res.data;
}

// ── sub-components ─────────────────────────────────────────────────

const providerColor: Record<string, string> = {
  openai: "text-green-400",
  anthropic: "text-orange-400",
  gemini: "text-blue-400",
  ollama: "text-purple-400",
  claude_agent: "text-amber-300",
};

function AgentCard({
  agent,
  selected,
  onClick,
}: {
  agent: AgentInfo;
  selected: boolean;
  onClick: () => void;
}) {
  return (
    <button
      onClick={onClick}
      className={`w-full text-left p-3 rounded-lg border transition-colors ${
        selected
          ? "border-primary bg-primary/10"
          : "border-border bg-card hover:border-primary/40"
      }`}
    >
      <div className="font-medium text-sm text-foreground">{agent.name}</div>
      <div className="text-xs text-muted-foreground mt-0.5">{agent.description}</div>
      <div className={`text-xs mt-1 ${providerColor[agent.default_provider] ?? "text-muted-foreground"}`}>
        {agent.default_provider}
      </div>
    </button>
  );
}

function ToolCallCard({ call }: { call: ToolCallEvent }) {
  const { t } = useTranslation();
  const statusColor =
    call.status === "running" ? "bg-amber-400 animate-pulse" :
    call.status === "error" ? "bg-red-500" :
    "bg-green-500";
  const argsStr = JSON.stringify(call.args, null, 2);
  return (
    <div className="border border-border/60 bg-muted/30 rounded-md p-2 text-xs my-1.5">
      <div className="flex items-center gap-2">
        <span className={`inline-block w-2 h-2 rounded-full ${statusColor}`} />
        <span className="font-mono text-amber-300">{call.name}</span>
        <span className="text-muted-foreground">
          {call.status === "running" ? t("ai.tool.calling") :
           call.status === "error" ? t("ai.tool.failed") : t("ai.tool.done")}
        </span>
      </div>
      <details className="mt-1.5">
        <summary className="cursor-pointer text-muted-foreground hover:text-foreground select-none">{t("ai.tool.args")}</summary>
        <pre className="mt-1 bg-background/60 border border-border rounded p-2 overflow-auto max-h-40 text-foreground/80">{argsStr}</pre>
      </details>
      {call.result && (
        <details className="mt-1">
          <summary className={`cursor-pointer hover:text-foreground select-none ${call.isError ? "text-red-400" : "text-muted-foreground"}`}>
            {t("ai.tool.result")}
          </summary>
          <pre className="mt-1 bg-background/60 border border-border rounded p-2 overflow-auto max-h-60 text-foreground/80 whitespace-pre-wrap">{call.result}</pre>
        </details>
      )}
    </div>
  );
}

function MessageBubble({ msg }: { msg: ChatMessage }) {
  const isUser = msg.role === "user";
  return (
    <div className={`flex ${isUser ? "justify-end" : "justify-start"}`}>
      <div
        className={`max-w-[78%] rounded-lg px-4 py-2.5 text-sm whitespace-pre-wrap leading-relaxed ${
          isUser
            ? "bg-primary text-primary-foreground"
            : "bg-card border border-border text-foreground"
        }`}
      >
        {!isUser && msg.toolCalls?.map((tc) => <ToolCallCard key={tc.id} call={tc} />)}
        {msg.content}
        {msg.streaming && (
          <span className="inline-block w-1.5 h-3.5 bg-current ml-0.5 animate-pulse align-middle" />
        )}
      </div>
    </div>
  );
}

// ── main page ──────────────────────────────────────────────────────

export default function AIPage() {
  const { t } = useTranslation();
  const token = useAuthStore((s) => s.token);
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

  const [selectedAgent, setSelectedAgent] = useState<string>(navState?.agentId ?? "");
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [context] = useState<Record<string, unknown>>(navState?.context ?? {});
  const [input, setInput] = useState(navState?.initialMessage ?? "");
  const [streaming, setStreaming] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [useClaudeAgent, setUseClaudeAgent] = useState(false);
  const bottomRef = useRef<HTMLDivElement>(null);
  const abortRef = useRef<AbortController | null>(null);
  const sentNavState = useRef(false);

  const canUseClaudeAgent = role === "analyst" || role === "admin";

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
  }

  async function sendMessage() {
    const text = input.trim();
    if (!text || streaming || !selectedAgent) return;

    setInput("");
    setError(null);
    const userMsg: ChatMessage = { role: "user", content: text };
    const history = [...messages, userMsg];
    setMessages([...history, { role: "assistant", content: "", streaming: true, toolCalls: [] }]);
    setStreaming(true);

    const activeSpec = agents.find((a) => a.id === selectedAgent);
    const effectiveProvider =
      useClaudeAgent && canUseClaudeAgent && activeSpec?.default_provider !== "claude_agent"
        ? "claude_agent"
        : undefined;

    const ctrl = new AbortController();
    abortRef.current = ctrl;
    let assembled = "";

    const updateLastAssistant = (mutator: (m: ChatMessage) => ChatMessage) => {
      setMessages((prev) => {
        const next = [...prev];
        const last = next[next.length - 1];
        if (last?.role === "assistant") {
          next[next.length - 1] = mutator(last);
        }
        return next;
      });
    };

    try {
      const resp = await fetch("/api/ai/chat", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          Authorization: `Bearer ${token}`,
        },
        body: JSON.stringify({
          agent_id: selectedAgent,
          messages: history.map((m) => ({ role: m.role, content: m.content })),
          context,
          provider: effectiveProvider,
        }),
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
            if (obj.error) { setError(obj.error); break; }
            if (obj.delta) {
              assembled += obj.delta;
              updateLastAssistant((m) => ({ ...m, content: assembled, streaming: true }));
            }
            if (obj.tool_call) {
              const tc: ToolCallEvent = {
                id: obj.tool_call.id,
                name: obj.tool_call.name,
                args: obj.tool_call.args,
                status: "running",
              };
              updateLastAssistant((m) => ({ ...m, toolCalls: [...(m.toolCalls ?? []), tc] }));
            }
            if (obj.tool_result) {
              updateLastAssistant((m) => ({
                ...m,
                toolCalls: (m.toolCalls ?? []).map((tc) =>
                  tc.id === obj.tool_result.id
                    ? { ...tc, result: obj.tool_result.summary,
                        isError: obj.tool_result.is_error,
                        status: obj.tool_result.is_error ? "error" : "done" }
                    : tc
                ),
              }));
            }
          } catch { /* ignore malformed */ }
        }
      }
    } catch (e: unknown) {
      if ((e as Error).name !== "AbortError") {
        setError((e as Error).message);
      }
    } finally {
      setStreaming(false);
      // Finalise: stop spinner on any tool still "running" (e.g. aborted mid-call)
      updateLastAssistant((m) => ({
        ...m,
        streaming: false,
        content: assembled || m.content,
        toolCalls: (m.toolCalls ?? []).map((tc) =>
          tc.status === "running" ? { ...tc, status: "error", result: tc.result ?? "cancelled" } : tc,
        ),
      }));
    }
  }

  function stopGeneration() {
    abortRef.current?.abort();
  }

  function clearChat() {
    if (streaming) return;
    setMessages([]);
    setError(null);
  }

  const activeAgent = agents.find((a) => a.id === selectedAgent);
  const effectiveIsClaudeAgent =
    activeAgent?.default_provider === "claude_agent" ||
    (useClaudeAgent && canUseClaudeAgent);

  return (
    <div className="min-h-screen bg-background flex">
      {/* ── sidebar: agent selector ─────────────────────────────── */}
      <aside className="w-64 border-r border-border flex flex-col p-4 gap-3 shrink-0">
        <div>
          <h2 className="text-sm font-semibold text-foreground">{t("ai.title")}</h2>
          <p className="text-xs text-muted-foreground mt-0.5">{t("ai.subtitle")}</p>
        </div>
        {isLoading ? (
          <p className="text-xs text-muted-foreground animate-pulse">{t("ai.loading")}</p>
        ) : (
          <div className="space-y-2">
            {agents.map((a) => (
              <AgentCard
                key={a.id}
                agent={a}
                selected={a.id === selectedAgent}
                onClick={() => switchAgent(a.id)}
              />
            ))}
          </div>
        )}
        <div className="mt-auto text-xs text-muted-foreground">
          <a href="/dashboard" className="hover:text-foreground transition-colors">{t("ai.back_dashboard")}</a>
        </div>
      </aside>

      {/* ── main chat area ──────────────────────────────────────── */}
      <div className="flex-1 flex flex-col">
        {/* header */}
        <header className="border-b border-border px-6 py-3 flex items-center justify-between">
          <div>
            <span className="font-medium text-foreground text-sm">
              {activeAgent?.name ?? t("ai.header_default")}
            </span>
            {activeAgent && (
              <span className="text-xs text-muted-foreground ml-2">{activeAgent.description}</span>
            )}
          </div>
          <div className="flex items-center gap-4">
            {canUseClaudeAgent && activeAgent && activeAgent.default_provider !== "claude_agent" && (
              <label className="flex items-center gap-2 text-xs text-muted-foreground cursor-pointer select-none"
                     title={t("ai.use_tools_hint")}>
                <input
                  type="checkbox"
                  checked={useClaudeAgent}
                  disabled={streaming}
                  onChange={(e) => setUseClaudeAgent(e.target.checked)}
                  className="accent-amber-400"
                />
                {t("ai.use_tools")}
              </label>
            )}
            {effectiveIsClaudeAgent && (
              <span className="text-xs text-amber-300" title={t("ai.tools_on_hint")}>
                {t("ai.tools_on")}
              </span>
            )}
            <button
              onClick={clearChat}
              disabled={streaming}
              className="text-xs text-muted-foreground hover:text-foreground transition-colors disabled:opacity-40"
            >
              {t("ai.clear_chat")}
            </button>
          </div>
        </header>

        {/* message list */}
        <div className="flex-1 overflow-y-auto px-6 py-4 space-y-4">
          {messages.length === 0 && (
            <div className="h-full flex items-center justify-center">
              <p className="text-sm text-muted-foreground">
                {activeAgent
                  ? t("ai.ask_placeholder", { agent: activeAgent.name })
                  : t("ai.select_to_begin")}
              </p>
            </div>
          )}
          {messages.map((msg, i) => (
            <MessageBubble key={i} msg={msg} />
          ))}
          {error && (
            <div className="text-xs text-red-400 bg-red-950/30 border border-red-900/50 rounded px-3 py-2">
              {error}
            </div>
          )}
          <div ref={bottomRef} />
        </div>

        {/* input bar */}
        <div className="border-t border-border px-6 py-4">
          <div className="flex gap-2">
            <textarea
              value={input}
              onChange={(e) => setInput(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter" && !e.shiftKey) {
                  e.preventDefault();
                  sendMessage();
                }
              }}
              placeholder={t("ai.input_placeholder")}
              rows={2}
              disabled={streaming || !selectedAgent}
              className="flex-1 resize-none bg-card border border-border rounded-md px-3 py-2 text-sm text-foreground placeholder:text-muted-foreground focus:outline-none focus:border-primary/50 disabled:opacity-50"
            />
            {streaming ? (
              <button
                onClick={stopGeneration}
                className="px-4 py-2 rounded-md bg-red-900/30 border border-red-800 text-red-400 text-sm hover:bg-red-900/50 transition-colors self-end"
              >
                {t("ai.stop")}
              </button>
            ) : (
              <button
                onClick={sendMessage}
                disabled={!input.trim() || !selectedAgent}
                className="px-4 py-2 rounded-md bg-primary text-primary-foreground text-sm font-medium hover:bg-primary/90 transition-colors disabled:opacity-50 self-end"
              >
                {t("ai.send")}
              </button>
            )}
          </div>
          <p className="text-xs text-muted-foreground mt-1.5">
            {t("ai.disclaimer")}
          </p>
        </div>
      </div>
    </div>
  );
}
