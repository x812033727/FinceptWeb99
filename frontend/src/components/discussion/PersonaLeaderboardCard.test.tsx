import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import api from "@/lib/api";

import { PersonaLeaderboardCard } from "./PersonaLeaderboardCard";
import type { PersonaLeaderboardResponse } from "./_helpers";

vi.mock("@/lib/api", () => ({ default: { get: vi.fn() } }));
const mockedApi = api as unknown as { get: ReturnType<typeof vi.fn> };

function renderCard(strategyId?: string) {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={qc}>
      <PersonaLeaderboardCard strategyId={strategyId} />
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  mockedApi.get.mockReset();
  // useCollapsible persists open/closed in localStorage; tests
  // share that storage in jsdom so reset to "default closed" state
  // between tests to keep behaviour deterministic.
  localStorage.clear();
});

const baseResponse: PersonaLeaderboardResponse = {
  owner_id: "u1", strategy_id: "s1", market: null, days: 90,
  items: [
    {
      persona_id: "buffett",
      participation_count: 10,
      win_attribution_count: 8,
      win_attribution_rate: 0.80,
      average_weight: 1.5,
      weight_trend_30d: 0.10,
      frozen_in_strategies: 0,
      shadow_in_strategies: 0,
    },
    {
      persona_id: "soros",
      participation_count: 10,
      win_attribution_count: 4,
      win_attribution_rate: 0.40,
      average_weight: 0.8,
      weight_trend_30d: -0.05,
      frozen_in_strategies: 1,
      shadow_in_strategies: 0,
    },
  ],
};

describe("PersonaLeaderboardCard", () => {
  it("starts collapsed (no fetch on mount)", () => {
    mockedApi.get.mockResolvedValue({ data: baseResponse });
    renderCard("s1");
    expect(mockedApi.get).not.toHaveBeenCalled();
  });

  it("expands and renders one row per persona", async () => {
    mockedApi.get.mockResolvedValue({ data: baseResponse });
    renderCard("s1");
    fireEvent.click(screen.getByRole("button", { name: /leaderboard/i }));
    await waitFor(() => {
      expect(screen.getByText("buffett")).toBeInTheDocument();
      expect(screen.getByText("soros")).toBeInTheDocument();
    });
  });

  it("hides weight columns when no strategy is scoped", async () => {
    mockedApi.get.mockResolvedValue({
      data: { ...baseResponse, strategy_id: null },
    });
    const { container } = renderCard();
    fireEvent.click(screen.getByRole("button", { name: /leaderboard/i }));
    await screen.findByText("buffett");
    // Weight column header shouldn't render
    expect(container.textContent).not.toMatch(/Avg weight/i);
  });

  it("clicking a header doesn't refetch (sort is client-side)", async () => {
    mockedApi.get.mockResolvedValue({ data: baseResponse });
    renderCard("s1");
    fireEvent.click(screen.getByRole("button", { name: /leaderboard/i }));
    // Wait for table content to render
    await screen.findByText("buffett");
    expect(mockedApi.get).toHaveBeenCalledTimes(1);
    fireEvent.click(screen.getByRole("button", { name: /Participation/i }));
    // Sort happens in-memory; no second fetch
    expect(mockedApi.get).toHaveBeenCalledTimes(1);
  });

  it("shows frozen / shadow status icons", async () => {
    mockedApi.get.mockResolvedValue({ data: baseResponse });
    renderCard("s1");
    fireEvent.click(screen.getByRole("button", { name: /leaderboard/i }));
    expect(await screen.findByTitle(/frozen in N strategies/i)).toBeInTheDocument();
  });
});
