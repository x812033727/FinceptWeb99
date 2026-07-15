import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const { submitOrder, cancelOrder, matchOrder, updateRisk, riskPolicy } = vi.hoisted(() => ({
  submitOrder: vi.fn(),
  cancelOrder: vi.fn(),
  matchOrder: vi.fn(),
  updateRisk: vi.fn(),
  riskPolicy: {
    trading_enabled: true,
    max_order_notional_usd: null,
    max_order_notional_twd: null,
    max_position_notional_usd: null,
    max_position_notional_twd: null,
    max_daily_loss_usd: null,
    max_daily_loss_twd: null,
    max_open_orders: null,
    max_symbol_concentration_pct: null,
    daily_realized_pnl_usd: -12.5,
    daily_realized_pnl_twd: 0,
  },
}));

vi.mock("@/hooks/usePortfolio", () => ({
  usePaperOrders: () => ({
    isLoading: false,
    data: [
      {
        id: "order-1", symbol: "AAPL", market: "US", side: "buy",
        order_type: "limit", time_in_force: "day", quantity: 10,
        filled_quantity: 4, limit_price: 100, reservation_price: 100,
        average_fill_price: 99, status: "partially_filled",
      },
    ],
  }),
  useSubmitPaperOrder: () => ({ mutateAsync: submitOrder, isPending: false, error: null }),
  useCancelPaperOrder: () => ({ mutate: cancelOrder, isPending: false, error: null }),
  useMatchPaperOrder: () => ({ mutate: matchOrder, isPending: false, error: null }),
  usePaperRiskPolicy: () => ({
    isLoading: false,
    data: riskPolicy,
  }),
  usePaperPerformance: () => ({
    isLoading: false,
    error: null,
    data: {
      total_fill_count: 3,
      window_fill_count: 3,
      truncated: false,
      summaries: [
        { currency: "USD", fill_count: 3, total_realized_pnl: 18.8, win_rate_pct: 50, profit_factor: 1.94, max_drawdown: -20, best_exit_pnl: 38.8, worst_exit_pnl: -20, total_fees: 4.2 },
        { currency: "TWD", fill_count: 0, total_realized_pnl: 0, win_rate_pct: null, profit_factor: null, max_drawdown: 0, best_exit_pnl: null, worst_exit_pnl: null, total_fees: 0 },
      ],
      curve: [
        { fill_id: "fill-1", currency: "USD", cumulative_realized_pnl: -3 },
        { fill_id: "fill-2", currency: "USD", cumulative_realized_pnl: 18.8 },
      ],
    },
  }),
  useUpdatePaperRiskPolicy: () => ({
    mutate: updateRisk,
    mutateAsync: updateRisk,
    isPending: false,
    error: null,
  }),
}));

import PaperTradingPanel from "./PaperTradingPanel";

describe("PaperTradingPanel", () => {
  beforeEach(() => {
    submitOrder.mockReset().mockResolvedValue({});
    cancelOrder.mockReset();
    matchOrder.mockReset();
    updateRisk.mockReset().mockResolvedValue({});
  });

  it("submits a typed order and exposes match and cancel actions", async () => {
    render(<PaperTradingPanel portfolioId="portfolio-1" />);

    expect(screen.getByText("Partially filled")).toBeInTheDocument();
    expect(screen.getByText("4 / 10")).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("Symbol"), { target: { value: " msft " } });
    fireEvent.change(screen.getByLabelText("Quantity"), { target: { value: "2" } });
    fireEvent.change(screen.getByLabelText("Price"), { target: { value: "410" } });
    fireEvent.click(screen.getByRole("button", { name: "Submit order" }));

    await waitFor(() => expect(submitOrder).toHaveBeenCalledWith(expect.objectContaining({
      symbol: "MSFT", market: "US", side: "buy", order_type: "limit",
      time_in_force: "day", quantity: 2, limit_price: 410,
    })));

    fireEvent.click(screen.getByRole("button", { name: "Match now" }));
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(matchOrder).toHaveBeenCalledWith("order-1");
    expect(cancelOrder).toHaveBeenCalledWith("order-1");
  });

  it("shows realized pnl and exposes the kill switch", () => {
    render(<PaperTradingPanel portfolioId="portfolio-1" />);

    expect(screen.getByText(/USD -12.5/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Engage kill switch" }));
    expect(updateRisk).toHaveBeenCalledWith(expect.objectContaining({
      trading_enabled: false,
      max_order_notional_usd: null,
      max_daily_loss_usd: null,
    }));
    expect(screen.getByText("+18.80 USD")).toBeInTheDocument();
    expect(screen.getByText("50.0%")).toBeInTheDocument();
    expect(screen.getByRole("img", { name: "USD Realized P&L curve" })).toBeInTheDocument();
  });
});
