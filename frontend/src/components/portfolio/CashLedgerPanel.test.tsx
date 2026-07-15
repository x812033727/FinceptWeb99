import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const { addMutate, reverseMutate } = vi.hoisted(() => ({
  addMutate: vi.fn(), reverseMutate: vi.fn(),
}));

vi.mock("@/hooks/usePortfolio", () => ({
  useCashBalance: () => ({
    data: {
      base_currency: "TWD", balances: { TWD: 120000, USD: -50 },
      total_cash_base: 118400, negative_currencies: ["USD"],
    },
  }),
  useCashEntries: () => ({
    isLoading: false,
    data: [{
      id: "entry-1", currency: "TWD", amount: 120000,
      entry_type: "deposit", source: "manual", occurred_on: "2026-07-15",
      reversal_of: null,
      is_reversed: false,
    }],
  }),
  useAddCashEntry: () => ({ mutateAsync: addMutate, isPending: false, isError: false }),
  useReverseCashEntry: () => ({ mutate: reverseMutate, isPending: false }),
}));

import CashLedgerPanel from "./CashLedgerPanel";

describe("CashLedgerPanel", () => {
  beforeEach(() => {
    addMutate.mockReset().mockResolvedValue({});
    reverseMutate.mockReset();
  });

  it("shows multi-currency balances and records append-only cash entries", async () => {
    render(<CashLedgerPanel portfolioId="portfolio-1" defaultCurrency="TWD" />);
    expect(screen.getByText("120,000")).toBeInTheDocument();
    expect(screen.getByText("-50")).toBeInTheDocument();
    expect(screen.getByText(/Negative cash balance in: USD/)).toBeInTheDocument();

    fireEvent.change(screen.getByLabelText("Amount"), { target: { value: "50000" } });
    fireEvent.click(screen.getAllByRole("button", { name: "Add cash entry" })[0]);
    await waitFor(() => expect(addMutate).toHaveBeenCalledWith(expect.objectContaining({
      currency: "TWD", amount: 50000, entry_type: "deposit",
    })));

    fireEvent.click(screen.getByRole("button", { name: "Reverse" }));
    expect(reverseMutate).toHaveBeenCalledWith("entry-1");
    expect(screen.getByText(/Entries cannot be edited or deleted/)).toBeInTheDocument();
  });
});
