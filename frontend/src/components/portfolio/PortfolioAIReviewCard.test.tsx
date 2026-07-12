/**
 * B5 AI 投組健檢 card tests.
 *
 * Covers: the section rendering (title + run button + empty hint),
 * the SSE stream flow (mocked fetch → ReadableStream → markdown
 * content appears, endpoint/auth shape asserted), and the
 * quota-exceeded / missing-API-key / empty-portfolio error states —
 * mirroring StockAIReportPanel's tests, minus the history list
 * (reviews are on-demand and not persisted).
 */
import { describe, it, expect, beforeEach, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";

const { notifyRateLimitedMock } = vi.hoisted(() => ({
  notifyRateLimitedMock: vi.fn(),
}));

vi.mock("@/lib/api", () => ({
  default: {
    get: vi.fn().mockResolvedValue({ data: {} }),
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

import { PortfolioAIReviewCard } from "./PortfolioAIReviewCard";

const REVIEW_MD =
  "## 總評\n投組體質穩健,規模適中,**+12.3%**。\n\n## 行動建議\n- 降低 AAPL 權重 — 集中度警示\n**本報告由 AI 產生,僅供研究參考,非投資建議。**";

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

function renderCard() {
  return render(<PortfolioAIReviewCard portfolioId="p-1" />);
}

beforeEach(() => {
  notifyRateLimitedMock.mockReset();
  vi.unstubAllGlobals();
});

// ── tests ──────────────────────────────────────────────────────────

describe("PortfolioAIReviewCard — section rendering", () => {
  it("renders the health-check section with run button and empty hint", () => {
    renderCard();
    expect(screen.getByTestId("portfolio-ai-review")).toBeInTheDocument();
    expect(screen.getByText("AI Health Check")).toBeInTheDocument();
    expect(screen.getByText("Run Health Check")).toBeInTheDocument();
    // Empty-state hint shows before any stream starts.
    expect(screen.getByText(/No review yet/)).toBeInTheDocument();
  });
});

describe("PortfolioAIReviewCard — streaming", () => {
  it("streams SSE deltas into the markdown pane", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      mockSSEResponse([
        'data: {"stage": "context"}\n\n',
        'data: {"stage": "generating"}\n\n',
        `data: ${JSON.stringify({ delta: REVIEW_MD.slice(0, 25) })}\n\n`,
        `data: ${JSON.stringify({ delta: REVIEW_MD.slice(25) })}\n\n`,
        'data: {"done": {"generated_at": "2026-07-12T00:00:00Z"}}\n\n',
        "data: [DONE]\n\n",
      ]),
    );
    vi.stubGlobal("fetch", fetchMock);

    renderCard();
    fireEvent.click(screen.getByText("Run Health Check"));

    // Heading from the streamed markdown appears rendered as a block.
    expect(await screen.findByText("總評")).toBeInTheDocument();
    await waitFor(() =>
      expect(screen.getByTestId("ai-review-content")).toHaveTextContent("行動建議"),
    );
    // Endpoint + auth header shape matches the SSE stream contract.
    expect(fetchMock).toHaveBeenCalledWith(
      "/api/ai/portfolio-review/p-1",
      expect.objectContaining({
        method: "POST",
        headers: expect.objectContaining({ Authorization: "Bearer tok" }),
      }),
    );
    // Disclaimer footer (with the not-saved note) shows once settled.
    expect(
      await screen.findByText(/not investment advice/),
    ).toBeInTheDocument();
    expect(screen.getByText(/not saved/)).toBeInTheDocument();
    // Button flips to the re-run label.
    expect(screen.getByText("Run Again")).toBeInTheDocument();
  });

  it("shows the quota-exceeded state and fires the rate-limit toast on 429", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        mockErrorResponse(429, "Daily AI quota exceeded (5 requests/day)."),
      ),
    );
    renderCard();
    fireEvent.click(screen.getByText("Run Health Check"));
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
    renderCard();
    fireEvent.click(screen.getByText("Run Health Check"));
    expect(
      await screen.findByText(
        "No LLM API key configured. Add a provider key in the admin console or your personal settings, then retry.",
      ),
    ).toBeInTheDocument();
  });

  it("shows the empty-portfolio state when the backend reports no holdings", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        mockSSEResponse([
          'data: {"stage": "context"}\n\n',
          'data: {"error": "Portfolio has no holdings to review"}\n\n',
          "data: [DONE]\n\n",
        ]),
      ),
    );
    renderCard();
    fireEvent.click(screen.getByText("Run Health Check"));
    expect(
      await screen.findByText(
        "This portfolio has no holdings yet. Add transactions first, then run the health check.",
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
    renderCard();
    fireEvent.click(screen.getByText("Run Health Check"));
    expect(await screen.findByText("provider exploded")).toBeInTheDocument();
  });
});
