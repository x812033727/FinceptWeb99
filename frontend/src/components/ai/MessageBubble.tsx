import { cn } from "@/lib/utils";
import { ToolCallCard } from "./ToolCallCard";
import type { ChatMessage } from "./types";

interface MessageBubbleProps {
  msg: ChatMessage;
  /** Optional class extension (e.g. for Discussion's persona-coloured headers). */
  className?: string;
}

// Single chat bubble. User messages right-aligned with primary fill;
// assistant messages left-aligned with card border. Tool-call cards
// render inside the assistant bubble before the streamed content so
// the user can watch tools fire while the LLM thinks. Streaming cursor
// (animated pulse line) appended at the very end while `streaming`.
export function MessageBubble({ msg, className }: MessageBubbleProps) {
  const isUser = msg.role === "user";
  return (
    <div className={cn("flex", isUser ? "justify-end" : "justify-start", className)}>
      <div
        className={cn(
          "max-w-[90%] sm:max-w-[78%] rounded-lg px-4 py-2.5 text-sm whitespace-pre-wrap leading-relaxed",
          isUser
            ? "bg-primary text-primary-foreground"
            : "bg-card border border-border text-foreground"
        )}
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
