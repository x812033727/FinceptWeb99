import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import HoldingsTable from "./HoldingsTable";
import type { Holding } from "@/types/portfolio";

function makeHolding(overrides: Partial<Holding> = {}): Holding {
  return {
    id: "h1",
    symbol: "AAPL",
    market: "US",
    quantity: 10,
    avg_cost: 180,
    cost_currency: "USD",
    current_price: 190,
    current_value: 1900,
    unrealized_pnl: 100,
    unrealized_pnl_pct: 5.56,
    weight_pct: 40,
    ...overrides,
  };
}

describe("HoldingsTable", () => {
  it("shows empty-state message when no holdings", () => {
    render(<HoldingsTable holdings={[]} currency="USD" />);
    expect(screen.getByText(/no holdings yet/i)).toBeInTheDocument();
  });

  it("renders one row per holding and shows the symbol", () => {
    render(
      <HoldingsTable
        holdings={[makeHolding({ symbol: "AAPL" }), makeHolding({ id: "h2", symbol: "MSFT" })]}
        currency="USD"
      />
    );
    expect(screen.getByText("AAPL")).toBeInTheDocument();
    expect(screen.getByText("MSFT")).toBeInTheDocument();
  });

  it("renders currency in the Value header", () => {
    render(<HoldingsTable holdings={[makeHolding()]} currency="TWD" />);
    expect(screen.getByText(/Value \(TWD\)/)).toBeInTheDocument();
  });

  it("shows positive P&L with '+' prefix", () => {
    render(<HoldingsTable holdings={[makeHolding({ unrealized_pnl: 123 })]} currency="USD" />);
    expect(screen.getByText("+123")).toBeInTheDocument();
  });

  it("shows negative P&L without '+' prefix", () => {
    render(<HoldingsTable holdings={[makeHolding({ unrealized_pnl: -50 })]} currency="USD" />);
    // negative numbers come with their own "-" sign via toLocaleString
    expect(screen.getByText("-50")).toBeInTheDocument();
  });

  it("tolerates missing current_price/unrealized_pnl without crashing", () => {
    const h: Holding = makeHolding({
      current_price: undefined,
      current_value: undefined,
      unrealized_pnl: undefined,
      unrealized_pnl_pct: undefined,
    });
    render(<HoldingsTable holdings={[h]} currency="USD" />);
    expect(screen.getByText("AAPL")).toBeInTheDocument();
    // fall-through values should render as formatted zeros
    expect(screen.getByText("+0.00%")).toBeInTheDocument();
  });
});
