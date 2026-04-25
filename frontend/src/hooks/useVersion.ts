import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import api from "@/lib/api";
import type { UpdateResult, VersionStatus } from "@/types/system";

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

export function useCheckForUpdates() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: () => api.post<VersionStatus>("/admin/version/check").then((r) => r.data),
    onSuccess: (data) => {
      qc.setQueryData(["version"], data);
    },
  });
}
