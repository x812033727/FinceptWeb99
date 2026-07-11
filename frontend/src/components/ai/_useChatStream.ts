/**
 * Chat streaming engine for AIPage: message list / input / streaming /
 * error state plus the SSE `sendMessage` loop and abort handling.
 * Extracted verbatim from `pages/AIPage.tsx` (PR-8 巨石頁拆分) — only
 * the closure variables became hook parameters; the bodies are
 * unchanged so behavior is identical.
 */
import { useRef, useState } from "react";
import { notifyRateLimited } from "@/lib/api";
import { useAuthStore } from "@/store/authStore";
import type { ChatMessage, ToolCallEvent } from "@/components/ai/types";
import type { AgentInfo } from "@/types/discussion";

export function useChatStream({
  agents,
  selectedAgent,
  context,
  useClaudeAgent,
  canUseClaudeAgent,
  refetchMe,
  initialInput,
}: {
  agents: AgentInfo[];
  selectedAgent: string;
  context: Record<string, unknown>;
  useClaudeAgent: boolean;
  canUseClaudeAgent: boolean;
  refetchMe: () => Promise<unknown>;
  initialInput: string;
}) {
  const token = useAuthStore((s) => s.token);
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [input, setInput] = useState(initialInput);
  const [streaming, setStreaming] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const abortRef = useRef<AbortController | null>(null);

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
      // Refresh quota counter once the stream settles.
      void refetchMe();
    }
  }

  function stopGeneration() {
    abortRef.current?.abort();
  }

  return {
    messages,
    setMessages,
    input,
    setInput,
    streaming,
    error,
    setError,
    sendMessage,
    stopGeneration,
  };
}
