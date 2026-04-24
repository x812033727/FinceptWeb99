import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import api from "@/lib/api";
import type { Portfolio, Transaction } from "@/types/portfolio";

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

export function useOptimise(portfolioId: string) {
  return useMutation({
    mutationFn: (body: { target_risk: string; max_weight: number }) =>
      api.post(`/portfolio/${portfolioId}/optimise`, body).then((r) => r.data),
  });
}
