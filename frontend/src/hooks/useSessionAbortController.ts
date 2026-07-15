import { useCallback, useEffect, useRef } from "react";
import { useAuthStore } from "@/store/authStore";

/**
 * Own one in-flight request that must not survive an auth identity boundary.
 * A new request replaces the previous controller; logout, account replacement,
 * and component unmount all abort it. Same-user token rotation is preserved.
 */
export function useSessionAbortController() {
  const userId = useAuthStore((state) => state.user?.id ?? null);
  const controllerRef = useRef<AbortController | null>(null);

  const abort = useCallback(() => {
    controllerRef.current?.abort();
    controllerRef.current = null;
  }, []);

  const renew = useCallback(() => {
    abort();
    const controller = new AbortController();
    controllerRef.current = controller;
    return controller;
  }, [abort]);

  useEffect(() => abort, [abort, userId]);

  return { abort, renew };
}
