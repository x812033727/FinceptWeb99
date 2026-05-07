import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import api from "@/lib/api";

import { PersonaStatusGrid } from "./PersonaStatusGrid";
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
    persona_ids: ["buffett", "lynch", "soros"],
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
    persona_status: {},
    ...over,
  };
}

function renderWith(strategy: StrategyTemplate) {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={qc}>
      <PersonaStatusGrid strategy={strategy} />
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  mockedApi.patch.mockReset();
});

describe("PersonaStatusGrid", () => {
  it("hides entirely when all personas are active", () => {
    const { container } = renderWith(makeStrategy());
    expect(container.firstChild).toBeNull();
  });

  it("renders one chip per non-active persona", () => {
    renderWith(makeStrategy({
      persona_status: { buffett: "frozen", soros: "shadow" },
    }));
    // The two non-active personas appear; the active 'lynch' doesn't
    expect(screen.getByText("buffett")).toBeInTheDocument();
    expect(screen.getByText("soros")).toBeInTheDocument();
    expect(screen.queryByText("lynch")).not.toBeInTheDocument();
  });

  it("frozen chip uses zinc tone + line-through", () => {
    const { container } = renderWith(makeStrategy({
      persona_status: { buffett: "frozen" },
    }));
    const chip = container.querySelector("span.line-through");
    expect(chip?.textContent).toContain("buffett");
  });

  it("shadow chip uses purple tone (no line-through)", () => {
    const { container } = renderWith(makeStrategy({
      persona_status: { soros: "shadow" },
    }));
    const purple = container.querySelector("span.text-purple-200");
    expect(purple?.textContent).toContain("soros");
  });

  it("Thaw button PATCHes to active", async () => {
    mockedApi.patch.mockResolvedValue({
      data: { strategy_id: "s1", persona_id: "buffett", status: "active" },
    });
    renderWith(makeStrategy({
      persona_status: { buffett: "frozen" },
    }));
    const thawBtn = screen.getByRole("button", { name: /thaw/i });
    fireEvent.click(thawBtn);
    await waitFor(() => {
      expect(mockedApi.patch).toHaveBeenCalledWith(
        "/discussion/strategies/s1/personas/buffett/status",
        { status: "active" },
      );
    });
  });
});
