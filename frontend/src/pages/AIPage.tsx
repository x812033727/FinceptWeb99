import { useState, useRef, useEffect } from "react";
import { useQuery } from "@tanstack/react-query";
import { useLocation } from "react-router-dom";
import api from "@/lib/api";
import { useAuthStore } from "@/store/authStore";

// ── types ──────────────────────────────────────────────────────────

interface AgentInfo {
  id: string;
  name: string;
  description: string;
  default_provider: string;
}

interface ChatMessage {
  role: "user" | "assistant";
  content: string;
  streaming?: boolean;
}

// ── api helpers ────────────────────────────────────────────────────

async function fetchAgents(): Promise<AgentInfo[]> {
  const res = await api.get<AgentInfo[]>("/ai/agents");
  return res.data;
}

// ── sub-components ─────────────────────────────────────────────────

function AgentCard({
  agent,
  selected,
  onClick,
}: {
  agent: AgentInfo;
  selected: boolean;
  onClick: () => void;
}) {
  const providerColor: Record<string, string> = {
    openai: "text-green-400",
    anthropic: "text-orange-400",
    gemini: "text-blue-400",
    ollama: "text-purple-400",
  };
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
  const token = useAuthStore((s) => s.token);
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
  const bottomRef = useRef<HTMLDivElement>(null);
  const abortRef = useRef<AbortController | null>(null);
  const sentNavState = useRef(false);

  // Select first agent once loaded (only if not set via nav state)
  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect
    if (agents.length && !selectedAgent) setSelectedAgent(agents[0].id);
  }, [agents, selectedAgent]);

  // Auto-send initial message from navigation state (e.g. from MacroPage)
  useEffect(() => {
    if (navState?.initialMessage && agents.length && selectedAgent && !sentNavState.current) {
      sentNavState.current = true;
      sendMessage();
    }
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [agents, selectedAgent]);

  // Scroll to bottom on new messages
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
    setMessages([...history, { role: "assistant", content: "", streaming: true }]);
    setStreaming(true);

    const ctrl = new AbortController();
    abortRef.current = ctrl;
    let assembled = "";

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
        }),
        signal: ctrl.signal,
      });

      if (!resp.ok) {
        const data = await resp.json().catch(() => ({}));
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
              setMessages((prev) => {
                const next = [...prev];
                next[next.length - 1] = { role: "assistant", content: assembled, streaming: true };
                return next;
              });
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
      setMessages((prev) => {
        const next = [...prev];
        if (next.length && next[next.length - 1].role === "assistant") {
          next[next.length - 1] = { role: "assistant", content: assembled || next[next.length - 1].content };
        }
        return next;
      });
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

  return (
    <div className="min-h-screen bg-background flex">
      {/* ── sidebar: agent selector ─────────────────────────────── */}
      <aside className="w-64 border-r border-border flex flex-col p-4 gap-3 shrink-0">
        <div>
          <h2 className="text-sm font-semibold text-foreground">AI Agents</h2>
          <p className="text-xs text-muted-foreground mt-0.5">Select a persona</p>
        </div>
        {isLoading ? (
          <p className="text-xs text-muted-foreground animate-pulse">Loading…</p>
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
          <a href="/dashboard" className="hover:text-foreground transition-colors">← Dashboard</a>
        </div>
      </aside>

      {/* ── main chat area ──────────────────────────────────────── */}
      <div className="flex-1 flex flex-col">
        {/* header */}
        <header className="border-b border-border px-6 py-3 flex items-center justify-between">
          <div>
            <span className="font-medium text-foreground text-sm">
              {activeAgent?.name ?? "Select an agent"}
            </span>
            {activeAgent && (
              <span className="text-xs text-muted-foreground ml-2">{activeAgent.description}</span>
            )}
          </div>
          <button
            onClick={clearChat}
            disabled={streaming}
            className="text-xs text-muted-foreground hover:text-foreground transition-colors disabled:opacity-40"
          >
            Clear chat
          </button>
        </header>

        {/* message list */}
        <div className="flex-1 overflow-y-auto px-6 py-4 space-y-4">
          {messages.length === 0 && (
            <div className="h-full flex items-center justify-center">
              <p className="text-sm text-muted-foreground">
                {activeAgent
                  ? `Ask the ${activeAgent.name} anything…`
                  : "Select an agent to begin"}
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
              placeholder="Ask a question… (Enter to send, Shift+Enter for newline)"
              rows={2}
              disabled={streaming || !selectedAgent}
              className="flex-1 resize-none bg-card border border-border rounded-md px-3 py-2 text-sm text-foreground placeholder:text-muted-foreground focus:outline-none focus:border-primary/50 disabled:opacity-50"
            />
            {streaming ? (
              <button
                onClick={stopGeneration}
                className="px-4 py-2 rounded-md bg-red-900/30 border border-red-800 text-red-400 text-sm hover:bg-red-900/50 transition-colors self-end"
              >
                Stop
              </button>
            ) : (
              <button
                onClick={sendMessage}
                disabled={!input.trim() || !selectedAgent}
                className="px-4 py-2 rounded-md bg-primary text-primary-foreground text-sm font-medium hover:bg-primary/90 transition-colors disabled:opacity-50 self-end"
              >
                Send
              </button>
            )}
          </div>
          <p className="text-xs text-muted-foreground mt-1.5">
            AI responses are for informational purposes only and not financial advice.
          </p>
        </div>
      </div>
    </div>
  );
}
