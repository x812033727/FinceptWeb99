/**
 * PR-3: tests for CalibrationReviewCard.
 *
 * Two layers covered:
 *   1. Empty state hides the entire card (no queued strategies → no
 *      AdminPage noise). This is the typical case so it must be
 *      bulletproof — a stray render crash here breaks the whole
 *      AdminPage.
 *   2. Queued state renders the diff table + reason + Approve /
 *      Reject buttons, and the buttons fire the right POST routes.
 */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import api from "@/lib/api";

import { CalibrationReviewCard } from "./CalibrationReviewCard";

vi.mock("@/lib/api", () => ({
  default: { get: vi.fn(), post: vi.fn() },
}));

const mockedApi = api as unknown as {
  get: ReturnType<typeof vi.fn>;
  post: ReturnType<typeof vi.fn>;
};

function renderCard() {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={qc}>
      <CalibrationReviewCard />
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  mockedApi.get.mockReset();
  mockedApi.post.mockReset();
});

describe("CalibrationReviewCard", () => {
  it("hides when no strategies have a pending curve", async () => {
    mockedApi.get.mockImplementation((url: string) => {
      if (url === "/discussion/strategies") {
        return Promise.resolve({
          data: [
            { id: "s1", name: "Strategy A", market: "TW" },
          ],
        });
      }
      // pending payload returns null pending
      return Promise.resolve({
        data: {
          strategy_id: "s1",
          live_curve: [{ raw: 0.5, calibrated: 0.4 }],
          live_updated_at: null,
          live_sample_count: 30,
          pending_curve: null,
          pending_reason: null,
          pending_at: null,
        },
      });
    });

    const { container } = renderCard();
    await waitFor(() => {
      // Card hides itself entirely when queue is empty
      expect(container.firstChild).toBeNull();
    });
  });

  it("renders queued strategy with diff table + reason", async () => {
    mockedApi.get.mockImplementation((url: string) => {
      if (url === "/discussion/strategies") {
        return Promise.resolve({
          data: [{ id: "s2", name: "Risky Strategy", market: "TW" }],
        });
      }
      return Promise.resolve({
        data: {
          strategy_id: "s2",
          live_curve: [
            { raw: 0.3, calibrated: 0.2 },
            { raw: 0.7, calibrated: 0.5 },
          ],
          live_updated_at: "2026-04-01T00:00:00Z",
          live_sample_count: 35,
          pending_curve: [
            { raw: 0.3, calibrated: 0.4 },
            { raw: 0.7, calibrated: 0.8 },
          ],
          pending_reason: "large shift vs prior curve (max Δ=0.300 > 0.30)",
          pending_at: "2026-05-07T00:00:00Z",
        },
      });
    });

    renderCard();
    expect(await screen.findByText("Risky Strategy")).toBeInTheDocument();
    // Reason text surfaces
    expect(screen.getByText(/large shift vs prior/)).toBeInTheDocument();
    // Diff table rows
    expect(screen.getByText("0.30")).toBeInTheDocument();
    expect(screen.getByText("0.70")).toBeInTheDocument();
    // Live + pending values
    expect(screen.getByText("0.200")).toBeInTheDocument();
    expect(screen.getByText("0.400")).toBeInTheDocument();
    expect(screen.getByText("0.500")).toBeInTheDocument();
    expect(screen.getByText("0.800")).toBeInTheDocument();
  });

  it("approve button POSTs the right route + invalidates the query", async () => {
    mockedApi.get.mockImplementation((url: string) => {
      if (url === "/discussion/strategies") {
        return Promise.resolve({
          data: [{ id: "s3", name: "Strat 3", market: "TW" }],
        });
      }
      return Promise.resolve({
        data: {
          strategy_id: "s3",
          live_curve: [],
          live_updated_at: null,
          live_sample_count: null,
          pending_curve: [{ raw: 0.5, calibrated: 0.5 }],
          pending_reason: "test",
          pending_at: null,
        },
      });
    });
    mockedApi.post.mockResolvedValue({ data: { promoted: true } });

    renderCard();
    const approveBtn = await screen.findByRole("button", { name: /approve/i });
    fireEvent.click(approveBtn);

    await waitFor(() => {
      expect(mockedApi.post).toHaveBeenCalledWith(
        "/discussion/strategies/s3/calibration/approve",
      );
    });
  });

  it("reject button POSTs the reject route", async () => {
    mockedApi.get.mockImplementation((url: string) => {
      if (url === "/discussion/strategies") {
        return Promise.resolve({
          data: [{ id: "s4", name: "Strat 4", market: "US" }],
        });
      }
      return Promise.resolve({
        data: {
          strategy_id: "s4",
          live_curve: [],
          live_updated_at: null,
          live_sample_count: null,
          pending_curve: [{ raw: 0.5, calibrated: 0.5 }],
          pending_reason: "test",
          pending_at: null,
        },
      });
    });
    mockedApi.post.mockResolvedValue({ data: { rejected: true } });

    renderCard();
    const rejectBtn = await screen.findByRole("button", { name: /reject/i });
    fireEvent.click(rejectBtn);

    await waitFor(() => {
      expect(mockedApi.post).toHaveBeenCalledWith(
        "/discussion/strategies/s4/calibration/reject",
      );
    });
  });
});
