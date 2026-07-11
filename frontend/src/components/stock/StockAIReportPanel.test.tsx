/**
 * B1 個股 AI 研究報告 panel tests.
 *
 * Covers: the new "AI Report" tab rendering inside StockDetailPage's
 * tab strip, the SSE stream flow (mocked fetch → ReadableStream →
 * markdown content appears), and the quota-exceeded / missing-API-key
 * error states that mirror AIPage's handling.
 */
import { describe, it, expect, beforeEach, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

const { apiGetMock, notifyRateLimitedMock } = vi.hoisted(() => ({
  apiGetMock: vi.fn(),
  notifyRateLimitedMock: vi.fn(),
}));

vi.mock("@/lib/api", () => ({
  default: {
    get: apiGetMock,
    post: vi.fn().mockResolvedValue({ data: {} }),
    delete: vi.fn().mockResolvedValue({ data: {} }),
  },
  notifyRateLimited: notifyRateLimitedMock,
}));

vi.mock("@/store/authStore", () => ({
  useAuthStore: Object.assign(
    <T,>(selector: (s: { token: string; user: { role: string } }) => T) =>
      selector({ token: "tok", user: { role: "viewer" } }),
    { getState: () => ({ token: "tok" }) },
  ),
}));

// Heavy page children not under test — the page render below only
// probes the tab strip + AI panel wiring.
vi.mock("@/components/charts/CandlestickChart", () => ({
  default: () => <div data-testid="chart" />,
}));
vi.mock("@/components/stock/LiveQuoteHeader", () => ({
  LiveQuoteHeader: () => <div data-testid="quote-header" />,
}));
vi.mock("@/components/stock/NewsFeed", () => ({
  NewsFeed: () => null,
}));

import { StockAIReportPanel } from "./StockAIReportPanel";
import StockDetailPage from "@/pages/StockDetailPage";

const REPORT_MD =
  "## 摘要\nTSMC 基本面穩健,外資 5 日買超 **+12.3%**。\n\n## 結論\n- 偏多觀察\n**本報告由 AI 產生,僅供研究參考,非投資建議。**";

// ── SSE fetch mock helpers ─────────────────────────────────────────

function sseBody(frames: string[]): ReadableStream<Uint8Array> {
  const encoder = new TextEncoder();
  return new ReadableStream({
    start(controller) {
      for (const f of frames) controller.enqueue(encoder.encode(f));
      controller.close();
    },
  });
}

function mockSSEResponse(frames: string[]): Response {
  return {
    ok: true,
    status: 200,
    body: sseBody(frames),
    headers: new Headers(),
    json: async () => ({}),
  } as unknown as Response;
}

function mockErrorResponse(status: number, detail: string): Response {
  return {
    ok: false,
    status,
    headers: new Headers(),
    json: async () => ({ detail }),
  } as unknown as Response;
}

// ── render helpers ─────────────────────────────────────────────────

function renderPanel() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <StockAIReportPanel symbol="2330" market="TW" />
    </QueryClientProvider>,
  );
}

function renderDetailPage(path = "/stock/TW/2330") {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={[path]}>
        <Routes>
          <Route path="/stock/:market/:symbol" element={<StockDetailPage />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  apiGetMock.mockReset();
  notifyRateLimitedMock.mockReset();
  apiGetMock.mockImplementation((url: string) => {
    if (url.startsWith("/ai/stock-reports")) return Promise.resolve({ data: [] });
    return Promise.resolve({ data: url.includes("/history/") ? [] : {} });
  });
  vi.unstubAllGlobals();
});

// ── tests ──────────────────────────────────────────────────────────

describe("StockDetailPage — AI Report tab", () => {
  // TabStrip collapses tabs 5+ into a "More" dropdown below md — force
  // the md+ branch so the new last tab renders inline for the probe.
  beforeEach(() => {
    window.matchMedia = vi.fn().mockImplementation((query: string) => ({
      matches: true,
      media: query,
      onchange: null,
      addListener: vi.fn(),
      removeListener: vi.fn(),
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
      dispatchEvent: vi.fn(),
    })) as unknown as typeof window.matchMedia;
  });

  it("renders the AI Report tab in the tab strip and shows the panel on click", async () => {
    renderDetailPage();
    const tab = await screen.findByRole("button", { name: "AI Report" });
    fireEvent.click(tab);
    expect(await screen.findByText("AI Research Report")).toBeInTheDocument();
    expect(screen.getByText("Generate Report")).toBeInTheDocument();
  });

  it("renders the AI Report tab for US symbols too", async () => {
    renderDetailPage("/stock/US/AAPL");
    expect(await screen.findByRole("button", { name: "AI Report" })).toBeInTheDocument();
  });
});

describe("StockAIReportPanel — streaming", () => {
  it("streams SSE deltas into the markdown pane and refreshes the saved list", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      mockSSEResponse([
        'data: {"stage": "context"}\n\n',
        'data: {"stage": "generating"}\n\n',
        `data: ${JSON.stringify({ delta: REPORT_MD.slice(0, 30) })}\n\n`,
        `data: ${JSON.stringify({ delta: REPORT_MD.slice(30) })}\n\n`,
        'data: {"done": {"report_id": "r-1", "created_at": "2026-07-11T00:00:00Z"}}\n\n',
        "data: [DONE]\n\n",
      ]),
    );
    vi.stubGlobal("fetch", fetchMock);

    renderPanel();
    fireEvent.click(screen.getByText("Generate Report"));

    // Heading from the streamed markdown appears rendered as a block.
    expect(await screen.findByText("摘要")).toBeInTheDocument();
    await waitFor(() =>
      expect(screen.getByTestId("ai-report-content")).toHaveTextContent("偏多觀察"),
    );
    // Endpoint + auth header shape matches the chat stream contract.
    expect(fetchMock).toHaveBeenCalledWith(
      "/api/ai/stock-report/TW/2330",
      expect.objectContaining({
        method: "POST",
        headers: expect.objectContaining({ Authorization: "Bearer tok" }),
      }),
    );
    // Disclaimer footer shows once streaming settles.
    expect(
      await screen.findByText(
        "This report is AI-generated, for research reference only — not investment advice.",
      ),
    ).toBeInTheDocument();
  });

  it("shows the quota-exceeded state and fires the rate-limit toast on 429", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        mockErrorResponse(429, "Daily AI quota exceeded (5 requests/day)."),
      ),
    );
    renderPanel();
    fireEvent.click(screen.getByText("Generate Report"));
    expect(
      await screen.findByText(
        "Daily AI quota exceeded. It resets at midnight UTC — please try again tomorrow.",
      ),
    ).toBeInTheDocument();
    expect(notifyRateLimitedMock).toHaveBeenCalled();
  });

  it("shows the missing-API-key state when the stream emits a key error", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        mockSSEResponse([
          'data: {"stage": "context"}\n\n',
          'data: {"error": "OpenAI API key not configured"}\n\n',
          "data: [DONE]\n\n",
        ]),
      ),
    );
    renderPanel();
    fireEvent.click(screen.getByText("Generate Report"));
    expect(
      await screen.findByText(
        "No LLM API key configured. Add a provider key in the admin console or your personal settings, then retry.",
      ),
    ).toBeInTheDocument();
  });

  it("shows a generic error box for other stream errors", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        mockSSEResponse([
          'data: {"error": "provider exploded"}\n\n',
          "data: [DONE]\n\n",
        ]),
      ),
    );
    renderPanel();
    fireEvent.click(screen.getByText("Generate Report"));
    expect(await screen.findByText("provider exploded")).toBeInTheDocument();
  });

  it("lists past reports and loads one on click", async () => {
    apiGetMock.mockImplementation((url: string) => {
      if (url.startsWith("/ai/stock-reports/r-9")) {
        return Promise.resolve({
          data: {
            id: "r-9", symbol: "2330", market: "TW", model: "gpt-4o-mini",
            created_at: "2026-07-10T02:00:00Z", preview: "台積電基本面穩健",
            content_md: REPORT_MD,
          },
        });
      }
      if (url.startsWith("/ai/stock-reports")) {
        return Promise.resolve({
          data: [{
            id: "r-9", symbol: "2330", market: "TW", model: "gpt-4o-mini",
            created_at: "2026-07-10T02:00:00Z", preview: "台積電基本面穩健",
          }],
        });
      }
      return Promise.resolve({ data: {} });
    });

    renderPanel();
    // Open the collapsible history list.
    fireEvent.click(await screen.findByText("Past Reports"));
    const item = await screen.findByText("台積電基本面穩健");
    fireEvent.click(item);
    await waitFor(() =>
      expect(screen.getByTestId("ai-report-content")).toHaveTextContent("偏多觀察"),
    );
  });
});
