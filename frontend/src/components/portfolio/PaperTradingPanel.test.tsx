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
  });
});
