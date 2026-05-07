import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { AxiosError } from "axios";
import api from "@/lib/api";
import type { DeployStatus, DeployTriggerResult } from "@/types/system";

// Polls /api/admin/deploy/status. The deploy itself recreates the
// backend container mid-flight, so 502 / network errors during
// `restarting` and `nginx` phases are EXPECTED — we silently retry
// and keep showing the last known phase.
export function useDeployStatus(enabled: boolean = true) {
  return useQuery({
    queryKey: ["deploy-status"],
    queryFn: () =>
      api.get<DeployStatus>("/admin/deploy/status").then((r) => r.data),
    enabled,
    refetchInterval: 2000,
    refetchIntervalInBackground: true,
    // Treat backend-down windows as "still deploying," not as errors.
    // Without `retry: true` here the query enters an error state on
    // the first 502 and stops polling; with it we keep trying every
    // 2s until the backend is back.
    retry: (_count, error) => {
      if (error instanceof AxiosError) {
        if (!error.response) return true; // network error
        if (error.response.status >= 500) return true;
      }
      return false;
    },
    retryDelay: 2000,
    // Keep showing the last successful payload while we retry, so the
    // UI doesn't flicker between "verifying" and "loading" mid-deploy.
    placeholderData: (prev) => prev,
  });
}

export function useTriggerDeploy() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: () =>
      api.post<DeployTriggerResult>("/admin/deploy").then((r) => r.data),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["deploy-status"] });
    },
  });
}
