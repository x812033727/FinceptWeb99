// Shared types for the chat / streaming UI primitives. Used by AIPage
// and (eventually) DiscussionPage's streaming overlay so both surfaces
// agree on the wire shape coming off the SSE stream.

export interface ToolCallEvent {
  id: string;
  name: string;
  args: unknown;
  result?: string;
  isError?: boolean;
  status: "running" | "done" | "error";
}

export interface ChatMessage {
  role: "user" | "assistant";
  content: string;
  streaming?: boolean;
  toolCalls?: ToolCallEvent[];
}
