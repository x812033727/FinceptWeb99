/**
 * Tests for the inline walk-forward trigger UI (PR-A1 follow-up
 * #341). The section is rendered inside each strategy row in
 * `StrategyTemplateCard`; it surfaces anchor_date / n_folds /
 * train+test windows / submit / inline result so an operator can
 * kick off the orchestrator from the UI instead of curl-ing the
 * backend endpoint.
 *
 * The orchestrator itself is fired-and-forgotten; we only assert
 * the UI calls `triggerWalkForward` with the right payload and
 * renders the resolved plan / error inline.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

const mockTrigger = vi.fn();
const mockFetchStrategies = vi.fn();

vi.mock("./_helpers", async () => {
  const actual = await vi.importActual<typeof import("./_helpers")>(
    "./_helpers",
  );
  return {
    ...actual,
    triggerWalkForward: (...args: unknown[]) => mockTrigger(...args),
    fetchStrategies: () => mockFetchStrategies(),
  };
});

import { StrategyTemplateCard } from "./StrategyTemplateCard";
import type { StrategyTemplate } from "./_helpers";

const STRATEGY: StrategyTemplate = {
  id: "00000000-0000-0000-0000-000000000001",
  name: "WF Test Strategy",
  description: null,
  topic: "topic",
  rules: "rules",
  market: "TW",
  persona_ids: ["buffett", "lynch"],
  default_rounds: 1,
  default_concurrency: 1,
  default_auto_post_mortem: true,
  persona_weights: {},
  weights_updated_at: null,
  auto_schedule_enabled: false,
  auto_schedule_cadence_hours: 24,
  auto_schedule_anchor_offset_days: -1,
  auto_schedule_trading_days_count: 1,
  auto_schedule_last_run_at: null,
  created_at: "2026-05-05T00:00:00Z",
  updated_at: "2026-05-05T00:00:00Z",
  deleted_at: null,
};

beforeEach(() => {
  localStorage.clear();
  // The card auto-collapses unless the panel is "open". Persist
  // the open state via the same key the card's `useCollapsible`
  // reads, so the strategy list (and the walk-forward section)
  // mount immediately.
  localStorage.setItem("discussion.strategy_panel", "1");
  mockTrigger.mockReset();
  mockFetchStrategies.mockReset();
  mockFetchStrategies.mockResolvedValue([STRATEGY]);
});

afterEach(() => {
  vi.clearAllMocks();
});

function renderCard() {
  const queryClient = new QueryClient({
    defaultOptions: {
      queries: { retry: false },
      mutations: { retry: false },
    },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <StrategyTemplateCard />
    </QueryClientProvider>,
  );
}

describe("WalkForwardSection", () => {
  it("hidden by default and reveals form on toggle click", async () => {
    renderCard();
    await waitFor(() =>
      expect(screen.getByText("WF Test Strategy")).toBeInTheDocument(),
    );

    // Form fields collapsed initially.
    expect(screen.queryByText(/Anchor.*test/i)).not.toBeInTheDocument();

    fireEvent.click(
      screen.getByRole("button", { name: /OOS|Walk-Forward/i }),
    );

    expect(await screen.findByText(/Anchor.*test/i)).toBeInTheDocument();
  });

  it("submits the configured payload to triggerWalkForward", async () => {
    mockTrigger.mockResolvedValue({
      strategy_id: STRATEGY.id,
      market: "TW",
      train_window_days: 60,
      test_window_days: 20,
      folds: [
        {
          fold_index: 0,
          train_anchor: "2026-02-01",
          train_dates: ["2026-02-01", "2026-04-01"],
          test_anchor: "2026-04-01",
          test_dates: ["2026-04-01", "2026-04-30"],
        },
      ],
      started: true,
    });

    renderCard();
    await waitFor(() =>
      expect(screen.getByText("WF Test Strategy")).toBeInTheDocument(),
    );

    fireEvent.click(
      screen.getByRole("button", { name: /OOS|Walk-Forward/i }),
    );

    const submit = await screen.findByRole("button", {
      name: /啟動 Walk-Forward|啟動.*Walk/i,
    });
    await act(async () => {
      fireEvent.click(submit);
    });

    await waitFor(() => expect(mockTrigger).toHaveBeenCalledTimes(1));
    const [strategyId, payload] = mockTrigger.mock.calls[0];
    expect(strategyId).toBe(STRATEGY.id);
    expect(payload).toMatchObject({
      train_window_days: 60,
      test_window_days: 20,
      n_folds: 2,
      rounds_per_discussion: 1,
      concurrency: 1,
      auto_post_mortem: true,
    });
    expect(payload.anchor_date).toMatch(/^\d{4}-\d{2}-\d{2}$/);

    await waitFor(() =>
      expect(screen.getByText(/已排定 1 個 fold/)).toBeInTheDocument(),
    );
  });

  it("renders backend error detail inline on 409 / 400", async () => {
    mockTrigger.mockRejectedValue({
      response: {
        data: {
          detail:
            "A walk-forward run is already in flight for this strategy.",
        },
      },
    });

    renderCard();
    await waitFor(() =>
      expect(screen.getByText("WF Test Strategy")).toBeInTheDocument(),
    );

    fireEvent.click(
      screen.getByRole("button", { name: /OOS|Walk-Forward/i }),
    );
    const submit = await screen.findByRole("button", {
      name: /啟動 Walk-Forward|啟動.*Walk/i,
    });
    await act(async () => {
      fireEvent.click(submit);
    });

    await waitFor(() =>
      expect(screen.getByText(/already in flight/i)).toBeInTheDocument(),
    );
  });
});
