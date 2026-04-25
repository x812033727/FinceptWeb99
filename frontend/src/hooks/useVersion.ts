import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import api from "@/lib/api";

export interface VersionStatus {
  current: string;
  latest: string;
  update_available: boolean;
  html_url: string;
  published_at: string;
}

export interface UpdateResult {
  status: "started" | "not_configured" | "failed";
  message: string;
}

export function useVersion() {
  return useQuery({
    queryKey: ["version"],
    queryFn: () => api.get<VersionStatus>("/system/version").then((r) => r.data),
    refetchInterval: 5 * 60 * 1000,
    staleTime: 60 * 1000,
  });
}

export function useTriggerUpdate() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: () => api.post<UpdateResult>("/admin/update").then((r) => r.data),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["version"] }),
  });
}
