/**
 * NLQueryBar (功能 B2) — submit a natural-language query, mock the
 * /api/ai/chat SSE stream with a run_screener tool result, and assert
 * the mapped rows reach the parent (rendered via a harness table) with
 * the assistant summary shown above.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { useState } from "react";

vi.mock("@/store/authStore", () => ({
  useAuthStore: <T,>(selector: (s: { token: string }) => T) =>
    selector({ token: "tok" }),
}));

const notifyRateLimited = vi.fn();
vi.mock("@/lib/api", () => ({
  default: {},
  notifyRateLimited: (...args: unknown[]) => notifyRateLimited(...args),
}));

vi.mock("react-i18next", () => ({
  useTranslation: () => ({
    t: (k: string) => k,
    i18n: { language: "en", exists: () => false },
  }),
}));

import type { NLScreenerResult } from "./NLQueryBar";
import { NLQueryBar } from "./NLQueryBar";

function sseResponse(frames: string[]): Response {
  const body = new ReadableStream<Uint8Array>({
    start(controller) {
      const enc = new TextEncoder();
      for (const f of frames) controller.enqueue(enc.encode(`data: ${f}\n\n`));
      controller.close();
    },
  });
  return new Response(body, {
    status: 200,
    headers: { "Content-Type": "text/event-stream" },
  });
}

/** Harness: renders the rows NLQueryBar hands back, like ScreenerPage
 * feeding ResultsTable. */
function Harness() {
  const [result, setResult] = useState<NLScreenerResult | null>(null);
  return (
    <div>
      <NLQueryBar onResult={setResult} />
      <ul data-testid="rows">
        {result?.rows.map((r) => (
          <li key={r.symbol}>{`${r.symbol}|${r.name}|${r.market}|${r.price}`}</li>
        ))}
      </ul>
    </div>
  );
}

const TOOL_RESULT_PAYLOAD = JSON.stringify({
  market: "TW",
  count: 2,
  rows: [
    {
      symbol: "2317", market: "TW", name_zh: "鴻海", price: 105.5,
      change_pct: 1.2, volume: 55_000_000, pe_ratio: 11.2,
      pb_ratio: 1.4, dividend_yield: 5.0, exchange: "TWSE",
      foreign_net_buy_days: 14,
    },
    {
      symbol: "2382", market: "TW", name_zh: "廣達", price: 250.0,
      change_pct: -0.5, volume: 12_000_000, pe_ratio: 18.9,
      pb_ratio: 4.1, dividend_yield: 3.2, exchange: "TWSE",
      foreign_net_buy_days: 12,
    },
  ],
});

const OK_FRAMES = [
  JSON.stringify({ tool_call: { id: "tu_1", name: "mcp__fincept__run_screener", args: { market: "TW" } } }),
  JSON.stringify({ tool_result: { id: "tu_1", name: "mcp__fincept__run_screener", summary: TOOL_RESULT_PAYLOAD, is_error: false } }),
  JSON.stringify({ delta: "找到 2 檔近月外資買超的電子股。" }),
  "[DONE]",
];

describe("NLQueryBar", () => {
  const fetchMock = vi.fn();

  beforeEach(() => {
    fetchMock.mockReset();
    notifyRateLimited.mockReset();
    vi.stubGlobal("fetch", fetchMock);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  async function submitQuery(text = "近月外資買超的電子股") {
    fireEvent.change(screen.getByLabelText("screener.nl.placeholder"), {
      target: { value: text },
    });
    fireEvent.click(screen.getByText("screener.nl.submit"));
  }

  it("submits, parses the run_screener tool result, and renders rows + summary", async () => {
    fetchMock.mockResolvedValue(sseResponse(OK_FRAMES));
    render(<Harness />);
    await submitQuery();

    await waitFor(() => {
      expect(screen.getByText("2317|鴻海|TW|105.5")).toBeTruthy();
    });
    expect(screen.getByText("2382|廣達|TW|250")).toBeTruthy();
    // assistant one-line summary shown
    expect(screen.getByText("找到 2 檔近月外資買超的電子股。")).toBeTruthy();
    // clear button appears once a result is active
    expect(screen.getByText("screener.nl.clear")).toBeTruthy();

    // request shape: tool-capable persona + system nudge context
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe("/api/ai/chat");
    const body = JSON.parse((init as RequestInit).body as string);
    expect(body.agent_id).toBe("claude_research");
    expect(body.messages).toEqual([
      { role: "user", content: "近月外資買超的電子股" },
    ]);
    expect(body.context.nl_screener_instruction).toContain("run_screener");
    expect((init as RequestInit).headers).toMatchObject({
      Authorization: "Bearer tok",
    });
  });

  it("clear button resets rows and summary", async () => {
    fetchMock.mockResolvedValue(sseResponse(OK_FRAMES));
    render(<Harness />);
    await submitQuery();
    await waitFor(() => expect(screen.getByText(/2317/)).toBeTruthy());

    fireEvent.click(screen.getByText("screener.nl.clear"));
    expect(screen.queryByText(/2317/)).toBeNull();
    expect(screen.queryByText(/找到 2 檔/)).toBeNull();
  });

  it("shows no_result error when the stream carries no run_screener result", async () => {
    fetchMock.mockResolvedValue(sseResponse([
      JSON.stringify({ delta: "我無法使用工具。" }),
      "[DONE]",
    ]));
    render(<Harness />);
    await submitQuery();
    await waitFor(() => {
      expect(screen.getByText("screener.nl.no_result")).toBeTruthy();
    });
    expect(screen.queryByTestId("rows")!.children.length).toBe(0);
  });

  it("surfaces stream errors from the SSE error frame", async () => {
    fetchMock.mockResolvedValue(sseResponse([
      JSON.stringify({ error: "ANTHROPIC_API_KEY not configured" }),
      "[DONE]",
    ]));
    render(<Harness />);
    await submitQuery();
    await waitFor(() => {
      expect(screen.getByText("ANTHROPIC_API_KEY not configured")).toBeTruthy();
    });
  });

  it("handles quota 429 via notifyRateLimited and shows the detail", async () => {
    fetchMock.mockResolvedValue(new Response(
      JSON.stringify({ detail: "Daily AI quota exceeded" }),
      { status: 429, headers: { "Content-Type": "application/json", "retry-after": "60" } },
    ));
    render(<Harness />);
    await submitQuery();
    await waitFor(() => {
      expect(screen.getByText("Daily AI quota exceeded")).toBeTruthy();
    });
    expect(notifyRateLimited).toHaveBeenCalledWith("Daily AI quota exceeded", 60);
  });
});
