import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import api from "@/lib/api";

import { AutoPromoteSettings } from "./AutoPromoteSettings";
import type { StrategyTemplate } from "./_helpers";

vi.mock("@/lib/api", () => ({
  default: { patch: vi.fn() },
}));

const mockedApi = api as unknown as { patch: ReturnType<typeof vi.fn> };

function makeStrategy(over: Partial<StrategyTemplate> = {}): StrategyTemplate {
  return {
    id: "s1",
    name: "x",
    description: null,
    topic: "t",
    rules: "r",
    market: "TW",
    persona_ids: ["a"],
    default_rounds: 1,
    default_concurrency: 1,
    default_auto_post_mortem: false,
    persona_weights: {},
    weights_updated_at: null,
    auto_schedule_enabled: false,
    auto_schedule_cadence_hours: 24,
    auto_schedule_anchor_offset_days: -1,
    auto_schedule_trading_days_count: 1,
    auto_schedule_last_run_at: null,
    created_at: "2026-05-07T00:00:00Z",
    updated_at: "2026-05-07T00:00:00Z",
    deleted_at: null,
    auto_promote_enabled: false,
    auto_promote_min_oos_brier_improvement: 0.0,
    auto_promote_min_oos_hit_rate: 0.5,
    ...over,
  };
}

function renderWith(strategy: StrategyTemplate) {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={qc}>
      <AutoPromoteSettings strategy={strategy} />
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  mockedApi.patch.mockReset();
});

describe("AutoPromoteSettings", () => {
  it("renders disabled checkbox by default + advanced fields hidden", () => {
    renderWith(makeStrategy());
    const cb = screen.getByRole("checkbox") as HTMLInputElement;
    expect(cb.checked).toBe(false);
    // Advanced fields hidden
    expect(screen.queryByLabelText(/min brier/i)).not.toBeInTheDocument();
  });

  it("expands advanced fields on toggle", () => {
    renderWith(makeStrategy());
    fireEvent.click(screen.getByText(/Advanced/));
    expect(screen.getByText(/Min Brier improvement/)).toBeInTheDocument();
    expect(screen.getByText(/Min hit rate/)).toBeInTheDocument();
  });

  it("save button only shows when dirty", () => {
    renderWith(makeStrategy());
    expect(screen.queryByRole("button", { name: /save/i })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("checkbox"));
    expect(screen.getByRole("button", { name: /save/i })).toBeInTheDocument();
  });

  it("PATCHes with the right payload", async () => {
    mockedApi.patch.mockResolvedValue({
      data: {
        strategy_id: "s1", auto_promote_enabled: true,
        min_brier_improvement: 0.0, min_hit_rate: 0.5,
      },
    });
    renderWith(makeStrategy());
    fireEvent.click(screen.getByRole("checkbox"));
    fireEvent.click(screen.getByRole("button", { name: /save/i }));
    await waitFor(() => {
      expect(mockedApi.patch).toHaveBeenCalledWith(
        "/discussion/strategies/s1/auto-promote",
        expect.objectContaining({
          enabled: true,
          min_brier_improvement: 0,
          min_hit_rate: 0.5,
        }),
      );
    });
  });
});
