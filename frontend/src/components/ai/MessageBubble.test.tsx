import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import { MessageBubble } from "./MessageBubble";

describe("MessageBubble", () => {
  it("renders user content right-aligned with primary fill", () => {
    const { container } = render(
      <MessageBubble msg={{ role: "user", content: "hi" }} />
    );
    const wrapper = container.firstChild as HTMLElement;
    expect(wrapper.className).toContain("justify-end");
    const bubble = wrapper.firstChild as HTMLElement;
    expect(bubble.className).toContain("bg-primary");
    expect(bubble.textContent).toContain("hi");
  });

  it("renders assistant content left-aligned with card border", () => {
    const { container } = render(
      <MessageBubble msg={{ role: "assistant", content: "hello" }} />
    );
    const wrapper = container.firstChild as HTMLElement;
    expect(wrapper.className).toContain("justify-start");
    const bubble = wrapper.firstChild as HTMLElement;
    expect(bubble.className).toContain("bg-card");
    expect(bubble.textContent).toContain("hello");
  });

  it("renders the streaming cursor when streaming=true", () => {
    const { container } = render(
      <MessageBubble msg={{ role: "assistant", content: "loading", streaming: true }} />
    );
    const cursor = container.querySelector(".animate-pulse");
    expect(cursor).not.toBeNull();
  });

  it("does NOT render the cursor when streaming=false", () => {
    const { container } = render(
      <MessageBubble msg={{ role: "assistant", content: "done" }} />
    );
    expect(container.querySelector(".animate-pulse")).toBeNull();
  });

  it("renders tool-call cards above the assistant text", () => {
    render(
      <MessageBubble
        msg={{
          role: "assistant",
          content: "Here is the analysis",
          toolCalls: [
            { id: "tc1", name: "run_dcf", args: { ticker: "AAPL" }, status: "running" },
          ],
        }}
      />
    );
    expect(screen.getByText("run_dcf")).toBeInTheDocument();
    expect(screen.getByText("Here is the analysis")).toBeInTheDocument();
  });

  it("does not render tool calls for user messages even if present", () => {
    // Defensive: user messages shouldn't have tool calls but if upstream
    // ever produces one we shouldn't render it on the user side.
    render(
      <MessageBubble
        msg={{
          role: "user",
          content: "do this",
          toolCalls: [
            { id: "tc1", name: "should_not_render", args: {}, status: "running" },
          ],
        }}
      />
    );
    expect(screen.queryByText("should_not_render")).toBeNull();
  });
});
