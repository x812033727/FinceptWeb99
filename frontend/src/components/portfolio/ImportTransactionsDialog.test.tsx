import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const { mutateAsync } = vi.hoisted(() => ({ mutateAsync: vi.fn() }));

vi.mock("@/hooks/usePortfolio", () => ({
  useImportTransactions: () => ({
    mutateAsync,
    isPending: false,
    isError: false,
    error: null,
  }),
}));

import { ImportTransactionsDialog } from "./ImportTransactionsDialog";

describe("ImportTransactionsDialog", () => {
  beforeEach(() => mutateAsync.mockReset());

  it("warns about an already imported batch and disables confirmation", async () => {
    mutateAsync.mockResolvedValue({
      valid: true,
      valid_count: 2,
      imported_count: 0,
      duplicate: true,
      import_id: "import-1",
      imported_at: "2026-07-15T14:00:00Z",
      errors: [],
    });
    render(
      <ImportTransactionsDialog portfolioId="portfolio-1" onClose={vi.fn()} />,
    );
    const csv = [
      "date,symbol,market,type,quantity,price",
      "2024-01-02,AAPL,US,buy,2,100",
      "2024-01-03,AAPL,US,sell,1,110",
    ].join("\n");
    const file = new File([csv], "transactions.csv", { type: "text/csv" });
    Object.defineProperty(file, "text", { value: async () => csv });
    const input = document.querySelector<HTMLInputElement>('input[type="file"]');
    expect(input).not.toBeNull();
    fireEvent.change(input!, { target: { files: [file] } });

    expect(await screen.findByText(
      "These 2 transactions were already imported. No data will be added again.",
    )).toBeInTheDocument();
    await waitFor(() => expect(mutateAsync).toHaveBeenCalledWith({
      rows: expect.any(Array), dry_run: true,
    }));
    expect(screen.getByRole("button", { name: "Import transactions" })).toBeDisabled();
  });
});
