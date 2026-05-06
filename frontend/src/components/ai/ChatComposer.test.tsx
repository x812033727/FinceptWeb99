import { describe, it, expect, vi, beforeEach } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import { ChatComposer } from "./ChatComposer";

const onChange = vi.fn();
const onSend = vi.fn();
const onStop = vi.fn();

beforeEach(() => {
  vi.clearAllMocks();
});

describe("ChatComposer", () => {
  it("renders Send button when not streaming", () => {
    render(
      <ChatComposer
        value="hi"
        onChange={onChange}
        onSend={onSend}
        onStop={onStop}
        streaming={false}
      />
    );
    expect(screen.getByLabelText("Send")).toBeInTheDocument();
    expect(screen.queryByLabelText("Stop")).toBeNull();
  });

  it("renders Stop button while streaming", () => {
    render(
      <ChatComposer
        value=""
        onChange={onChange}
        onSend={onSend}
        onStop={onStop}
        streaming={true}
      />
    );
    expect(screen.getByLabelText("Stop")).toBeInTheDocument();
    expect(screen.queryByLabelText("Send")).toBeNull();
  });

  it("Send is disabled when value is empty / whitespace", () => {
    const { rerender } = render(
      <ChatComposer
        value=""
        onChange={onChange}
        onSend={onSend}
        onStop={onStop}
        streaming={false}
      />
    );
    expect((screen.getByLabelText("Send") as HTMLButtonElement).disabled).toBe(true);
    rerender(
      <ChatComposer
        value="   "
        onChange={onChange}
        onSend={onSend}
        onStop={onStop}
        streaming={false}
      />
    );
    expect((screen.getByLabelText("Send") as HTMLButtonElement).disabled).toBe(true);
  });

  it("Enter (no shift) calls onSend when not streaming", () => {
    render(
      <ChatComposer
        value="hi"
        onChange={onChange}
        onSend={onSend}
        onStop={onStop}
        streaming={false}
      />
    );
    const ta = screen.getByRole("textbox");
    fireEvent.keyDown(ta, { key: "Enter", shiftKey: false });
    expect(onSend).toHaveBeenCalledTimes(1);
  });

  it("Shift+Enter does NOT send (newline)", () => {
    render(
      <ChatComposer
        value="hi"
        onChange={onChange}
        onSend={onSend}
        onStop={onStop}
        streaming={false}
      />
    );
    const ta = screen.getByRole("textbox");
    fireEvent.keyDown(ta, { key: "Enter", shiftKey: true });
    expect(onSend).not.toHaveBeenCalled();
  });

  it("Enter while streaming does NOT send", () => {
    render(
      <ChatComposer
        value="hi"
        onChange={onChange}
        onSend={onSend}
        onStop={onStop}
        streaming={true}
      />
    );
    const ta = screen.getByRole("textbox");
    fireEvent.keyDown(ta, { key: "Enter", shiftKey: false });
    expect(onSend).not.toHaveBeenCalled();
  });

  it("Stop button calls onStop", () => {
    render(
      <ChatComposer
        value=""
        onChange={onChange}
        onSend={onSend}
        onStop={onStop}
        streaming={true}
      />
    );
    fireEvent.click(screen.getByLabelText("Stop"));
    expect(onStop).toHaveBeenCalledTimes(1);
  });

  it("typing fires onChange with the new value", () => {
    render(
      <ChatComposer
        value="hi"
        onChange={onChange}
        onSend={onSend}
        onStop={onStop}
        streaming={false}
      />
    );
    fireEvent.change(screen.getByRole("textbox"), { target: { value: "hello" } });
    expect(onChange).toHaveBeenCalledWith("hello");
  });

  it("renders disclaimer when provided", () => {
    render(
      <ChatComposer
        value=""
        onChange={onChange}
        onSend={onSend}
        onStop={onStop}
        streaming={false}
        disclaimerText="Not financial advice"
      />
    );
    expect(screen.getByText("Not financial advice")).toBeInTheDocument();
  });
});
