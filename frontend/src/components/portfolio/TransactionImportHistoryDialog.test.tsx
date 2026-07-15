import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({
  mutateAsync: vi.fn(),
  reset: vi.fn(),
  batches: [] as Array<{
    id: string;
    row_count: number;
    linked_count: number;
    provenance_complete: boolean;
    imported_at: string;
  }>,
  rollbackError: null as Error | null,
}));

vi.mock("@/hooks/usePortfolio", () => ({
  useTransactionImports: () => ({
    data: mocks.batches,
    isLoading: false,
    isError: false,
    error: null,
  }),
  useRollbackTransactionImport: () => ({
    mutateAsync: mocks.mutateAsync,
    reset: mocks.reset,
    isPending: false,
    isError: mocks.rollbackError !== null,
    error: mocks.rollbackError,
  }),
}));

import { TransactionImportHistoryDialog } from "./TransactionImportHistoryDialog";

describe("TransactionImportHistoryDialog", () => {
  beforeEach(() => {
    mocks.mutateAsync.mockReset();
    mocks.reset.mockReset();
    mocks.mutateAsync.mockResolvedValue({ import_id: "import-1", removed_count: 2 });
    mocks.rollbackError = null;
    mocks.batches = [
      {
        id: "import-1",
        row_count: 2,
        linked_count: 2,
        provenance_complete: true,
        imported_at: "2026-07-15T14:00:00Z",
      },
      {
        id: "legacy-import",
        row_count: 3,
        linked_count: 0,
        provenance_complete: false,
        imported_at: "2026-07-14T14:00:00Z",
      },
    ];
  });

  it("confirms a complete rollback and disables incomplete provenance", async () => {
    render(
      <TransactionImportHistoryDialog portfolioId="portfolio-1" onClose={vi.fn()} />,
    );

    expect(screen.getByText("Source links complete (2/2)")).toBeInTheDocument();
    expect(screen.getByText(
      "Source links incomplete (0/3). This batch cannot be rolled back safely.",
    )).toBeInTheDocument();
    const rollbackButtons = screen.getAllByRole("button", { name: "Roll back" });
    expect(rollbackButtons[0]).toBeEnabled();
    expect(rollbackButtons[1]).toBeDisabled();

    fireEvent.click(rollbackButtons[0]);
    expect(screen.getByText(
      "Roll back all 2 transactions in this batch? Cash settlements will be reversed and affected holdings rebuilt.",
    )).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Confirm rollback" }));
    await waitFor(() => expect(mocks.mutateAsync).toHaveBeenCalledWith("import-1"));
  });

  it("surfaces a dependent-trade conflict from the API", () => {
    mocks.rollbackError = new Error(
      "Sell quantity exceeds available shares at transaction date",
    );
    render(
      <TransactionImportHistoryDialog portfolioId="portfolio-1" onClose={vi.fn()} />,
    );
    expect(screen.getByText(
      "Sell quantity exceeds available shares at transaction date",
    )).toBeInTheDocument();
  });
});
