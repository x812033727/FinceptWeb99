import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import api from "@/lib/api";
import type {
  CashBalance,
  CashEntry,
  Portfolio,
  PaperOrder,
  PaperOrderCreate,
  PaperFill,
  PaperPerformance,
  PaperRiskPolicy,
  PaperRiskPolicyUpdate,
  PortfolioRisk,
  Transaction,
} from "@/types/portfolio";

function invalidatePaperAccount(qc: ReturnType<typeof useQueryClient>, portfolioId: string) {
  return Promise.all([
    qc.invalidateQueries({ queryKey: ["paper-orders", portfolioId] }),
    qc.invalidateQueries({ queryKey: ["paper-fills", portfolioId] }),
    qc.invalidateQueries({ queryKey: ["paper-performance", portfolioId] }),
    qc.invalidateQueries({ queryKey: ["portfolio", portfolioId] }),
    qc.invalidateQueries({ queryKey: ["portfolio-cash", portfolioId] }),
    qc.invalidateQueries({ queryKey: ["portfolio-cash-entries", portfolioId] }),
  ]);
}

export function usePortfolios() {
  return useQuery({
    queryKey: ["portfolios"],
    queryFn: () => api.get<{ id: string; name: string; currency: string }[]>("/portfolio").then((r) => r.data),
  });
}

export function usePortfolioDetail(id: string | null) {
  return useQuery({
    queryKey: ["portfolio", id],
    queryFn: () => api.get<Portfolio>(`/portfolio/${id}`).then((r) => r.data),
    enabled: !!id,
    refetchInterval: 60_000,
  });
}

export function usePortfolioRisk(id: string | null) {
  return useQuery({
    queryKey: ["portfolio-risk", id],
    queryFn: () => api.get<PortfolioRisk>(`/portfolio/${id}/risk`).then((r) => r.data),
    enabled: !!id,
    // Server computes VaR (incl. 10k-sim Monte Carlo) on demand —
    // hold results 5 min; the panel exposes a manual refresh button.
    staleTime: 300_000,
  });
}

export function useCreatePortfolio() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (data: { name: string; currency: string }) =>
      api.post("/portfolio", data).then((r) => r.data),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["portfolios"] }),
  });
}

export function useDeletePortfolio() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => api.delete(`/portfolio/${id}`),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["portfolios"] }),
  });
}

export function useAddTransaction(portfolioId: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (tx: Omit<Transaction, "id">) =>
      api.post(`/portfolio/${portfolioId}/transaction`, tx).then((r) => r.data),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["portfolio", portfolioId] }),
  });
}

export interface TransactionImportResult {
  valid: boolean;
  valid_count: number;
  imported_count: number;
  duplicate: boolean;
  import_id: string | null;
  imported_at: string | null;
  errors: { row: number; field: string | null; message: string }[];
}

export interface TransactionImportBatch {
  id: string;
  row_count: number;
  linked_count: number;
  provenance_complete: boolean;
  first_tx_date: string | null;
  last_tx_date: string | null;
  instruments: { symbol: string; market: string }[];
  imported_at: string;
}

export function useImportTransactions(portfolioId: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: { rows: Record<string, unknown>[]; dry_run: boolean }) =>
      api.post<TransactionImportResult>(
        `/portfolio/${portfolioId}/transactions/import`, body,
      ).then((response) => response.data),
    onSuccess: (result) => {
      if (!result.imported_count) return;
      qc.invalidateQueries({ queryKey: ["portfolio", portfolioId] });
      qc.invalidateQueries({ queryKey: ["portfolio-transactions", portfolioId] });
      qc.invalidateQueries({ queryKey: ["portfolio-cash", portfolioId] });
      qc.invalidateQueries({ queryKey: ["portfolio-cash-entries", portfolioId] });
      qc.invalidateQueries({ queryKey: ["portfolio-transaction-imports", portfolioId] });
    },
  });
}

export function useTransactionImports(portfolioId: string) {
  return useQuery({
    queryKey: ["portfolio-transaction-imports", portfolioId],
    queryFn: () => api.get<TransactionImportBatch[]>(
      `/portfolio/${portfolioId}/transaction-imports`,
    ).then((response) => response.data),
    enabled: !!portfolioId,
  });
}

export interface TransactionImportTransaction {
  id: string;
  import_id: string | null;
  symbol: string;
  market: string;
  tx_type: string;
  quantity: number;
  price: number;
  fx_rate: number;
  tx_date: string;
  notes: string | null;
  created_at: string;
}

function fetchTransactionImportTransactions(portfolioId: string, importId: string) {
  return api.get<TransactionImportTransaction[]>(
    `/portfolio/${portfolioId}/transaction-imports/${importId}/transactions`,
  ).then((response) => response.data);
}

export function useTransactionImportTransactions(
  portfolioId: string, importId: string | null,
) {
  return useQuery({
    queryKey: ["portfolio-transaction-import", portfolioId, importId],
    queryFn: () => fetchTransactionImportTransactions(portfolioId, importId!),
    enabled: Boolean(importId),
  });
}

export function useExportTransactionImport(portfolioId: string) {
  return useMutation({
    mutationFn: (importId: string) =>
      fetchTransactionImportTransactions(portfolioId, importId),
  });
}

export interface PortfolioTransactionFilters {
  symbol?: string;
  market?: Transaction["market"];
  tx_type?: Transaction["tx_type"];
  date_from?: string;
  date_to?: string;
}

export interface PortfolioTransactionPage {
  items: TransactionImportTransaction[];
  next_cursor: string | null;
  total_count: number | null;
}

export function useExportPortfolioTransactions(portfolioId: string) {
  return useMutation({
    mutationFn: async (filters: PortfolioTransactionFilters = {}) => {
      const transactions: TransactionImportTransaction[] = [];
      let cursor: string | null = null;
      while (true) {
        const page: PortfolioTransactionPage = await api.get<PortfolioTransactionPage>(
          `/portfolio/${portfolioId}/transactions/page`,
          { params: { limit: 500, ...filters, ...(cursor ? { cursor } : {}) } },
        ).then((response) => response.data);
        transactions.push(...page.items);
        if (!page.next_cursor) return transactions;
        cursor = page.next_cursor;
      }
    },
  });
}

export function useRollbackTransactionImport(portfolioId: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (importId: string) => api.delete<{
      import_id: string; removed_count: number;
    }>(`/portfolio/${portfolioId}/transaction-imports/${importId}`).then(
      (response) => response.data,
    ),
    onSuccess: () => Promise.all([
      qc.invalidateQueries({ queryKey: ["portfolio-transaction-imports", portfolioId] }),
      qc.invalidateQueries({ queryKey: ["portfolio-transactions", portfolioId] }),
      qc.invalidateQueries({ queryKey: ["portfolio", portfolioId] }),
      qc.invalidateQueries({ queryKey: ["portfolio-cash", portfolioId] }),
      qc.invalidateQueries({ queryKey: ["portfolio-cash-entries", portfolioId] }),
    ]),
  });
}

export function useUpdatePortfolio(portfolioId: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (patch: { name?: string; currency?: string }) =>
      api.patch(`/portfolio/${portfolioId}`, patch).then((r) => r.data),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["portfolios"] });
      qc.invalidateQueries({ queryKey: ["portfolio", portfolioId] });
    },
  });
}

// Patch is loose because callers pass UI strings ('US' | 'CRYPTO') for market
// where the strict Transaction type expects an enum; the backend re-validates
// against the same Pydantic regex used for create.
export interface TransactionPatch {
  symbol?: string;
  market?: string;
  tx_type?: string;
  quantity?: number;
  price?: number;
  fx_rate?: number;
  tx_date?: string;
  notes?: string;
}

export function useUpdateTransaction(portfolioId: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ txId, patch }: { txId: string; patch: TransactionPatch }) =>
      api.patch(`/portfolio/${portfolioId}/transactions/${txId}`, patch).then((r) => r.data),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["portfolio", portfolioId] });
      qc.invalidateQueries({ queryKey: ["portfolio-transactions", portfolioId] });
      qc.invalidateQueries({ queryKey: ["portfolio-transaction-imports", portfolioId] });
      qc.invalidateQueries({ queryKey: ["portfolio-cash", portfolioId] });
      qc.invalidateQueries({ queryKey: ["portfolio-cash-entries", portfolioId] });
    },
  });
}

export function useDeleteTransaction(portfolioId: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (txId: string) =>
      api.delete(`/portfolio/${portfolioId}/transactions/${txId}`),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["portfolio", portfolioId] });
      qc.invalidateQueries({ queryKey: ["portfolio-transactions", portfolioId] });
      qc.invalidateQueries({ queryKey: ["portfolio-transaction-imports", portfolioId] });
      qc.invalidateQueries({ queryKey: ["portfolio-cash", portfolioId] });
      qc.invalidateQueries({ queryKey: ["portfolio-cash-entries", portfolioId] });
    },
  });
}

export function useOptimise(portfolioId: string) {
  return useMutation({
    mutationFn: (body: { target_risk: string; max_weight: number }) =>
      api.post(`/portfolio/${portfolioId}/optimise`, body).then((r) => r.data),
  });
}

export function useCashBalance(portfolioId: string) {
  return useQuery({
    queryKey: ["portfolio-cash", portfolioId],
    queryFn: () => api.get<CashBalance>(`/portfolio/${portfolioId}/cash`).then((r) => r.data),
    enabled: !!portfolioId,
  });
}

export function useCashEntries(portfolioId: string) {
  return useQuery({
    queryKey: ["portfolio-cash-entries", portfolioId],
    queryFn: () => api.get<CashEntry[]>(`/portfolio/${portfolioId}/cash-entries`).then((r) => r.data),
    enabled: !!portfolioId,
  });
}

export function useAddCashEntry(portfolioId: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: {
      currency: string; amount: number; entry_type: string;
      occurred_on: string; notes?: string;
    }) => api.post(`/portfolio/${portfolioId}/cash-entries`, {
      ...body,
      idempotency_key: `manual-${Date.now()}-${crypto.randomUUID()}`,
    }).then((r) => r.data),
    onSuccess: () => Promise.all([
      qc.invalidateQueries({ queryKey: ["portfolio-cash", portfolioId] }),
      qc.invalidateQueries({ queryKey: ["portfolio-cash-entries", portfolioId] }),
      qc.invalidateQueries({ queryKey: ["portfolio", portfolioId] }),
    ]),
  });
}

export function useReverseCashEntry(portfolioId: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (entryId: string) => api.post(
      `/portfolio/${portfolioId}/cash-entries/${entryId}/reverse`, {},
    ).then((r) => r.data),
    onSuccess: () => Promise.all([
      qc.invalidateQueries({ queryKey: ["portfolio-cash", portfolioId] }),
      qc.invalidateQueries({ queryKey: ["portfolio-cash-entries", portfolioId] }),
      qc.invalidateQueries({ queryKey: ["portfolio", portfolioId] }),
    ]),
  });
}

export function usePaperOrders(portfolioId: string) {
  return useQuery({
    queryKey: ["paper-orders", portfolioId],
    queryFn: () => api.get<PaperOrder[]>(`/portfolio/${portfolioId}/paper-orders`).then((r) => r.data),
    enabled: !!portfolioId,
    refetchInterval: 15_000,
  });
}

export function usePaperFills(
  portfolioId: string,
  orderId: string,
  enabled = true,
) {
  return useQuery({
    queryKey: ["paper-fills", portfolioId, orderId],
    queryFn: () => api.get<PaperFill[]>(
      `/portfolio/${portfolioId}/paper-orders/${orderId}/fills`,
    ).then((r) => r.data),
    enabled: enabled && !!portfolioId && !!orderId,
  });
}

export function usePaperRiskPolicy(portfolioId: string) {
  return useQuery({
    queryKey: ["paper-risk-policy", portfolioId],
    queryFn: () => api.get<PaperRiskPolicy>(
      `/portfolio/${portfolioId}/paper-risk-policy`,
    ).then((r) => r.data),
    enabled: !!portfolioId,
    refetchInterval: 15_000,
  });
}

export function usePaperPerformance(portfolioId: string) {
  return useQuery({
    queryKey: ["paper-performance", portfolioId],
    queryFn: () => api.get<PaperPerformance>(
      `/portfolio/${portfolioId}/paper-performance`,
    ).then((r) => r.data),
    enabled: !!portfolioId,
    refetchInterval: 15_000,
  });
}

export function useUpdatePaperRiskPolicy(portfolioId: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: PaperRiskPolicyUpdate) => api.put<PaperRiskPolicy>(
      `/portfolio/${portfolioId}/paper-risk-policy`, body,
    ).then((r) => r.data),
    onSuccess: () => Promise.all([
      qc.invalidateQueries({ queryKey: ["paper-risk-policy", portfolioId] }),
      invalidatePaperAccount(qc, portfolioId),
    ]),
  });
}

export function useSubmitPaperOrder(portfolioId: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: PaperOrderCreate) => api.post<PaperOrder>(
      `/portfolio/${portfolioId}/paper-orders`,
      { ...body, idempotency_key: `ui-order-${crypto.randomUUID()}` },
    ).then((r) => r.data),
    onSuccess: () => invalidatePaperAccount(qc, portfolioId),
  });
}

export function useCancelPaperOrder(portfolioId: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (orderId: string) => api.post<PaperOrder>(
      `/portfolio/${portfolioId}/paper-orders/${orderId}/cancel`, {},
    ).then((r) => r.data),
    onSuccess: () => invalidatePaperAccount(qc, portfolioId),
  });
}

export function useMatchPaperOrder(portfolioId: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (orderId: string) => api.post(
      `/portfolio/${portfolioId}/paper-orders/${orderId}/match`, {},
    ).then((r) => r.data),
    onSuccess: () => invalidatePaperAccount(qc, portfolioId),
  });
}
