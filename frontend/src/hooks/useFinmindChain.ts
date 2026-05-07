import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import api from "@/lib/api";

export interface FinmindChainState {
  status: "idle" | "running" | "stopping";
  queue: string[];
  current_dataset: string | null;
  current_symbol: string | null;
  chunks_done: number;
  chunks_total: number;
  chunks_failed: number;
  started_at: string | null;
  last_chunk_at: string | null;
  stop_requested: boolean;
  recent_errors: string[];
  selected_datasets: string[];
  universe_size: number;
  quota_used: number | null;
  quota_limit: number;
  external_activity_detected: boolean;
  default_datasets: string[];
  total_chunks_done: number;
  total_chunks_total: number;
}

export interface StartChainBody {
  datasets: string[];
  days?: number;
  reset_stuck_first?: boolean;
}

export function useFinmindChain(enabled: boolean) {
  const qc = useQueryClient();

  // Adaptive polling: 3s while the chain is moving (the user wants
  // a live feel), 30s when idle (mostly waiting on a button click).
  const stateQuery = useQuery<FinmindChainState>({
    queryKey: ["admin", "finmind", "chain"],
    queryFn: async () =>
      (await api.get<FinmindChainState>("/admin/finmind/chain")).data,
    enabled,
    refetchInterval: (q) => {
      const s = q.state.data?.status;
      return s === "running" || s === "stopping" ? 3000 : 30000;
    },
    placeholderData: (prev) => prev,
  });

  const start = useMutation({
    mutationFn: async (body: StartChainBody) =>
      (await api.post<FinmindChainState>("/admin/finmind/chain/start", body))
        .data,
    onSuccess: (data) => {
      qc.setQueryData(["admin", "finmind", "chain"], data);
    },
  });

  const stop = useMutation({
    mutationFn: async () =>
      (await api.post<FinmindChainState>("/admin/finmind/chain/stop")).data,
    onSuccess: (data) => {
      qc.setQueryData(["admin", "finmind", "chain"], data);
    },
  });

  const resetStuck = useMutation({
    mutationFn: async () =>
      (await api.post<{ reset: number }>("/admin/finmind/chain/reset-stuck"))
        .data,
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["admin", "finmind", "chain"] });
    },
  });

  return { stateQuery, start, stop, resetStuck };
}
