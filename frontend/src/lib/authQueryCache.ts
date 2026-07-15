import type { QueryClient } from "@tanstack/react-query";
import { useAuthStore } from "@/store/authStore";

/**
 * Keep query data inside one authenticated identity boundary.
 *
 * Query keys intentionally omit the current user id because ownership is
 * enforced by the API. That makes a full cache reset mandatory whenever the
 * authenticated user changes or signs out; otherwise a later session can
 * briefly reuse owner-scoped data from the previous account. Access-token
 * rotation keeps the same user id and therefore must not disturb the cache.
 */
export function clearQueryCacheOnAuthChange(queryClient: QueryClient) {
  let userId = useAuthStore.getState().user?.id ?? null;

  return useAuthStore.subscribe((state) => {
    const nextUserId = state.user?.id ?? null;
    if (nextUserId === userId) return;

    userId = nextUserId;
    queryClient.clear();
  });
}
