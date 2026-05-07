import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import api from "@/lib/api";

import { StrategyTimelineCard } from "./StrategyTimelineCard";
import type { StrategyTimeline } from "./_helpers";

vi.mock("@/lib/api", () => ({
  default: { get: vi.fn() },
}));

const mockedApi = api as unknown as { get: ReturnType<typeof vi.fn> };

function renderCard(strategyId = "s1") {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={qc}>
      <StrategyTimelineCard strategyId={strategyId} />
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  mockedApi.get.mockReset();
});

describe("StrategyTimelineCard", () => {
  it("starts collapsed and only fetches on expand", () => {
    mockedApi.get.mockResolvedValue({
      data: { strategy_id: "s1", days: 90, metrics: [], events: [] } as StrategyTimeline,
    });
    renderCard();
    // Collapsed → no API call yet
    expect(mockedApi.get).not.toHaveBeenCalled();
  });

  it("fetches when expanded and shows empty message on cold-start strategy", async () => {
    mockedApi.get.mockResolvedValue({
      data: { strategy_id: "s1", days: 90, metrics: [], events: [] } as StrategyTimeline,
    });
    renderCard();
    fireEvent.click(screen.getByRole("button", { name: /Timeline/ }));
    await waitFor(() => {
      expect(mockedApi.get).toHaveBeenCalledWith(
        "/discussion/strategies/s1/timeline?days=90",
      );
    });
    expect(await screen.findByText(/No events or metrics yet/i)).toBeInTheDocument();
  });

  it("renders the event-count chip when events exist", async () => {
    mockedApi.get.mockResolvedValue({
      data: {
        strategy_id: "s1", days: 90,
        metrics: [
          {
            date: "2026-04-01", brier_30d: 0.20, calibrated_brier_30d: 0.18,
            hit_rate_30d: 0.55, sample_count: 12, lesson_hit_rate_30d: null,
            maturity_tier: "learning", status_flags: [],
          },
        ],
        events: [
          {
            date: "2026-04-01T12:00:00+00:00",
            kind: "sweep_completed", sweep_id: "sw1",
            fold_kind: "production",
            discussions_completed: 5, discussions_failed: 0,
          },
          {
            date: "2026-04-02T12:00:00+00:00",
            kind: "version_change", artifact: "weights",
            version_number: 3, status: "active",
            trigger: "auto_promote",
            notes: "auto-promoted from walk-forward test fold",
          },
        ],
      } as StrategyTimeline,
    });
    renderCard();
    fireEvent.click(screen.getByRole("button", { name: /Timeline/ }));
    // The collapsible row shows "· 2 events"
    expect(await screen.findByText(/2 events/i)).toBeInTheDocument();
  });

  it("opens the event detail panel when a legend chip is clicked", async () => {
    mockedApi.get.mockResolvedValue({
      data: {
        strategy_id: "s1", days: 90,
        metrics: [
          {
            date: "2026-04-01", brier_30d: 0.20, calibrated_brier_30d: 0.18,
            hit_rate_30d: 0.55, sample_count: 12, lesson_hit_rate_30d: null,
            maturity_tier: "learning", status_flags: [],
          },
        ],
        events: [
          {
            date: "2026-04-01T12:00:00+00:00",
            kind: "version_change", artifact: "weights",
            version_number: 3, status: "active",
            trigger: "auto_promote",
            notes: "auto-promoted from walk-forward test fold",
          },
        ],
      } as StrategyTimeline,
    });
    renderCard();
    fireEvent.click(screen.getByRole("button", { name: /Timeline/ }));
    const chip = await screen.findByRole("button", { name: /Version change/i });
    fireEvent.click(chip);
    expect(await screen.findByText(/auto-promoted from walk-forward/i)).toBeInTheDocument();
    // Close button works
    fireEvent.click(screen.getByRole("button", { name: /close/i }));
    await waitFor(() => {
      expect(screen.queryByText(/auto-promoted from walk-forward/i)).not.toBeInTheDocument();
    });
  });
});
