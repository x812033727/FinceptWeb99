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

// ── Numeric formatting ──────────────────────────────────────────

describe("HoldingsTable — formatting depth", () => {
  it("renders quantity with locale thousands separators", () => {
    const h = makeHolding({ quantity: 1234567 });
    render(<HoldingsTable holdings={[h]} currency="USD" />);
    expect(screen.getByText("1,234,567")).toBeInTheDocument();
  });

  it("rounds avg_cost and current_price to 2 decimals", () => {
    const h = makeHolding({ avg_cost: 150.456, current_price: 200.789 });
    render(<HoldingsTable holdings={[h]} currency="USD" />);
    expect(screen.getByText("150.46")).toBeInTheDocument();
    expect(screen.getByText("200.79")).toBeInTheDocument();
  });

  it("rounds current_value to 0 decimals (the column gets large numbers)", () => {
    const h = makeHolding({ current_value: 12345.67 });
    render(<HoldingsTable holdings={[h]} currency="USD" />);
    expect(screen.getByText("12,346")).toBeInTheDocument();
  });

  it("formats PnL % with two decimals + sign for both positive and negative", () => {
    render(
      <HoldingsTable
        holdings={[
          makeHolding({ id: "a", unrealized_pnl_pct: 33.333 }),
          makeHolding({ id: "b", unrealized_pnl_pct: -12.5 }),
        ]}
        currency="USD"
      />
    );
    expect(screen.getByText("+33.33%")).toBeInTheDocument();
    expect(screen.getByText("-12.50%")).toBeInTheDocument();
  });

  it("formats weight_pct with one decimal", () => {
    render(<HoldingsTable holdings={[makeHolding({ weight_pct: 42.555 })]} currency="USD" />);
    expect(screen.getByText("42.6%")).toBeInTheDocument();
  });
});

// ── Colour classes ──────────────────────────────────────────────

describe("HoldingsTable — colour classes", () => {
  it("applies text-positive to PnL and PnL% cells when value is non-negative", () => {
    const { container } = render(
      <HoldingsTable holdings={[makeHolding({ unrealized_pnl: 500, unrealized_pnl_pct: 5 })]} currency="USD" />
    );
    const pnlCell = Array.from(container.querySelectorAll("td"))
      .find((td) => td.textContent === "+500");
    const pctCell = Array.from(container.querySelectorAll("td"))
      .find((td) => td.textContent === "+5.00%");
    expect(pnlCell?.className).toContain("text-positive");
    expect(pctCell?.className).toContain("text-positive");
  });

  it("applies text-negative when PnL value is negative", () => {
    const { container } = render(
      <HoldingsTable holdings={[makeHolding({ unrealized_pnl: -100, unrealized_pnl_pct: -2 })]} currency="USD" />
    );
    const pnlCell = Array.from(container.querySelectorAll("td"))
      .find((td) => td.textContent === "-100");
    const pctCell = Array.from(container.querySelectorAll("td"))
      .find((td) => td.textContent === "-2.00%");
    expect(pnlCell?.className).toContain("text-negative");
    expect(pctCell?.className).toContain("text-negative");
  });

  it("treats undefined PnL as 0 (positive branch)", () => {
    const { container } = render(
      <HoldingsTable holdings={[makeHolding({ unrealized_pnl: undefined, unrealized_pnl_pct: undefined })]} currency="USD" />
    );
    const pctCell = Array.from(container.querySelectorAll("td"))
      .find((td) => td.textContent === "+0.00%");
    // 0 >= 0, so the positive class wins.
    expect(pctCell?.className).toContain("text-positive");
  });
});

// ── Responsive column hiding (PR #38) ───────────────────────────

describe("HoldingsTable — RWD column hiding", () => {
  function tableHeaders(container: HTMLElement) {
    return Array.from(container.querySelectorAll("thead th"));
  }

  it("Symbol / Quantity / Value / PnL % are visible at every breakpoint", () => {
    const { container } = render(<HoldingsTable holdings={[makeHolding()]} currency="USD" />);
    const headers = tableHeaders(container);
    for (const label of ["Symbol", "Qty", "Value", "P&L %"]) {
      const th = headers.find((h) => h.textContent?.includes(label));
      expect(th, `header missing: ${label}`).toBeTruthy();
      expect(th!.className).not.toContain("hidden");
    }
  });

  it("Price column is hidden on `<sm:` (`hidden sm:table-cell`)", () => {
    const { container } = render(<HoldingsTable holdings={[makeHolding()]} currency="USD" />);
    const priceTh = tableHeaders(container).find((th) => th.textContent === "Price")!;
    expect(priceTh.className).toContain("hidden");
    expect(priceTh.className).toContain("sm:table-cell");
  });

  it("PnL and Weight columns are hidden on `<md:` (`hidden md:table-cell`)", () => {
    const { container } = render(<HoldingsTable holdings={[makeHolding()]} currency="USD" />);
    const headers = tableHeaders(container);
    for (const label of ["P&L", "Weight"]) {
      const th = headers.find((h) => h.textContent === label)!;
      expect(th.className).toContain("hidden");
      expect(th.className).toContain("md:table-cell");
    }
  });

  it("Market and Avg Cost columns are hidden on `<lg:` (`hidden lg:table-cell`)", () => {
    const { container } = render(<HoldingsTable holdings={[makeHolding()]} currency="USD" />);
    const headers = tableHeaders(container);
    for (const label of ["Market", "Avg Cost"]) {
      const th = headers.find((h) => h.textContent === label)!;
      expect(th.className).toContain("hidden");
      expect(th.className).toContain("lg:table-cell");
    }
  });

  it("each data <td> mirrors its header's hidden / breakpoint tokens (column alignment)", () => {
    const { container } = render(<HoldingsTable holdings={[makeHolding()]} currency="USD" />);
    const headers = tableHeaders(container);
    const cells = Array.from(container.querySelectorAll("tbody tr:first-child td"));
    expect(cells.length).toBe(headers.length);

    const tokens = ["hidden", "sm:table-cell", "md:table-cell", "lg:table-cell"];
    headers.forEach((th, i) => {
      const td = cells[i];
      for (const tok of tokens) {
        expect(td.className.includes(tok)).toBe(th.className.includes(tok));
      }
    });
  });
});

// ── Typography ──────────────────────────────────────────────────

describe("HoldingsTable — typography", () => {
  it("tbody carries `tabular-nums` so digits align across rows", () => {
    const { container } = render(<HoldingsTable holdings={[makeHolding()]} currency="USD" />);
    expect(container.querySelector("tbody")?.className).toContain("tabular-nums");
  });
});
