/**
 * Web Push subscription helpers (D3 瀏覽器推播).
 *
 * The SettingsPage toggle drives these; kept out of the component so
 * the browser-API choreography (permission → pushManager.subscribe →
 * backend persist) is unit-testable with mocked globals.
 */
import api from "@/lib/api";
import { useAuthStore } from "@/store/authStore";

export type PushStatus =
  | "on"            // active subscription exists
  | "off"           // supported, permitted or promptable, not subscribed
  | "denied"        // Notification permission blocked at browser level
  | "unsupported"   // browser lacks SW/Push/Notification APIs
  | "unconfigured"; // server has no VAPID keys

/** Standard conversion: base64url applicationServerKey → Uint8Array
 * for `pushManager.subscribe`. */
export function urlBase64ToUint8Array(base64String: string): Uint8Array<ArrayBuffer> {
  const padding = "=".repeat((4 - (base64String.length % 4)) % 4);
  const base64 = (base64String + padding).replace(/-/g, "+").replace(/_/g, "/");
  const raw = atob(base64);
  // Explicit ArrayBuffer backing so the result satisfies BufferSource
  // (pushManager.subscribe rejects SharedArrayBuffer-backed views).
  const output = new Uint8Array(new ArrayBuffer(raw.length));
  for (let i = 0; i < raw.length; i++) output[i] = raw.charCodeAt(i);
  return output;
}

export function isPushSupported(): boolean {
  return (
    typeof Notification !== "undefined" &&
    typeof navigator !== "undefined" &&
    "serviceWorker" in navigator &&
    typeof window !== "undefined" &&
    "PushManager" in window
  );
}

/** Current toggle state, cheap enough to run on Settings mount. */
export async function getPushStatus(): Promise<PushStatus> {
  if (!isPushSupported()) return "unsupported";
  if (Notification.permission === "denied") return "denied";
  try {
    const reg = await navigator.serviceWorker.ready;
    const sub = await reg.pushManager.getSubscription();
    return sub ? "on" : "off";
  } catch {
    return "off";
  }
}

/**
 * Full enable flow: permission prompt → fetch the server's VAPID
 * public key → pushManager.subscribe → persist on the backend.
 * Returns the resulting status ("denied"/"unconfigured" are expected
 * user-visible outcomes, not errors); throws on network/API failure.
 */
export async function enablePush(): Promise<PushStatus> {
  if (!isPushSupported()) return "unsupported";

  const permission = await Notification.requestPermission();
  if (permission !== "granted") return "denied";

  const { data } = await api.get("/notifications/vapid-public-key");
  if (!data?.public_key) return "unconfigured";

  const reg = await navigator.serviceWorker.ready;
  const existing = await reg.pushManager.getSubscription();
  const sub =
    existing ??
    (await reg.pushManager.subscribe({
      userVisibleOnly: true,
      applicationServerKey: urlBase64ToUint8Array(data.public_key),
    }));

  const json = sub.toJSON();
  await api.post("/notifications/push-subscribe", {
    endpoint: json.endpoint,
    keys: json.keys,
    user_agent: navigator.userAgent.slice(0, 255),
  });
  return "on";
}

/**
 * Disable flow: unsubscribe in the browser first (the user's intent
 * must win even if the backend call fails), then best-effort delete
 * the server row so the transport stops pushing at a dead endpoint.
 */
export async function disablePush(): Promise<PushStatus> {
  if (!isPushSupported()) return "unsupported";
  const reg = await navigator.serviceWorker.ready;
  const sub = await reg.pushManager.getSubscription();
  if (!sub) return "off";
  const endpoint = sub.endpoint;
  await sub.unsubscribe();
  try {
    await api.delete("/notifications/push-subscribe", { data: { endpoint } });
  } catch {
    // Backend row is now stale — the web_push transport prunes it on
    // the first 404/410 from the push service.
  }
  return "off";
}

/**
 * Remove the browser-side subscription without making an authenticated API
 * call. This is the safe fallback after a session has already expired: the
 * backend row may linger until its next 404/410 delivery, but the signed-out
 * browser can no longer receive another user's notifications.
 */
export async function unsubscribePushLocally(): Promise<void> {
  if (!isPushSupported()) return;
  const reg = await navigator.serviceWorker.ready;
  const sub = await reg.pushManager.getSubscription();
  await sub?.unsubscribe();
}

/** Prevent a device push subscription from crossing user identities. */
export function clearPushSubscriptionOnAuthChange() {
  let userId = useAuthStore.getState().user?.id ?? null;
  return useAuthStore.subscribe((state) => {
    const nextUserId = state.user?.id ?? null;
    if (userId !== null && nextUserId !== userId) {
      void unsubscribePushLocally().catch(() => undefined);
    }
    userId = nextUserId;
  });
}
