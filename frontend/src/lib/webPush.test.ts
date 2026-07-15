/**
 * Tests for the Web Push helper choreography (D3): permission →
 * VAPID key fetch → pushManager.subscribe → backend persist, plus the
 * unsupported/denied/unconfigured off-ramps. Browser push APIs are
 * absent in jsdom, so they're stubbed wholesale.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";

const { apiMock } = vi.hoisted(() => ({
  apiMock: {
    get: vi.fn(),
    post: vi.fn(),
    delete: vi.fn(),
  },
}));
vi.mock("@/lib/api", () => ({ default: apiMock }));

import {
  clearPushSubscriptionOnAuthChange,
  disablePush,
  enablePush,
  getPushStatus,
  isPushSupported,
  unsubscribePushLocally,
  urlBase64ToUint8Array,
} from "./webPush";
import { useAuthStore } from "@/store/authStore";

// ── browser API stubs ─────────────────────────────────────────────

function makeFakeSubscription(endpoint = "https://push.example.com/sub/1") {
  return {
    endpoint,
    toJSON: () => ({ endpoint, keys: { p256dh: "BKey", auth: "Auth" } }),
    unsubscribe: vi.fn().mockResolvedValue(true),
  };
}

function stubPushEnv({
  permission = "default" as NotificationPermission,
  requestPermissionResult = "granted" as NotificationPermission,
  subscription = null as ReturnType<typeof makeFakeSubscription> | null,
} = {}) {
  const pushManager = {
    getSubscription: vi.fn().mockResolvedValue(subscription),
    subscribe: vi.fn().mockResolvedValue(makeFakeSubscription()),
  };
  const registration = { pushManager };

  vi.stubGlobal("Notification", {
    permission,
    requestPermission: vi.fn().mockResolvedValue(requestPermissionResult),
  });
  vi.stubGlobal("PushManager", class {});
  Object.defineProperty(window.navigator, "serviceWorker", {
    value: { ready: Promise.resolve(registration) },
    configurable: true,
  });
  return { pushManager, registration };
}

beforeEach(() => {
  vi.clearAllMocks();
  useAuthStore.getState().clearAuth();
  apiMock.get.mockResolvedValue({ data: { configured: true, public_key: "QUJDRA" } });
  apiMock.post.mockResolvedValue({ data: {} });
  apiMock.delete.mockResolvedValue({ data: {} });
});

afterEach(() => {
  vi.unstubAllGlobals();
  // @ts-expect-error test cleanup of the defined property
  delete window.navigator.serviceWorker;
});

// ── urlBase64ToUint8Array ─────────────────────────────────────────

describe("urlBase64ToUint8Array", () => {
  it("decodes plain base64url", () => {
    expect(Array.from(urlBase64ToUint8Array("AQID"))).toEqual([1, 2, 3]);
  });

  it("handles url-safe chars and missing padding", () => {
    // 0xfb 0xef 0xbe → base64 "++++" / base64url "----"; "_" maps to "/".
    expect(Array.from(urlBase64ToUint8Array("-----w"))).toEqual([
      0xfb, 0xef, 0xbe, 0xfb,
    ]);
    // Length not a multiple of 4 → padding restored internally.
    expect(Array.from(urlBase64ToUint8Array("QQ"))).toEqual([0x41]);
  });
});

// ── support / status detection ────────────────────────────────────

describe("getPushStatus", () => {
  it("is unsupported in a bare jsdom environment", async () => {
    expect(isPushSupported()).toBe(false);
    expect(await getPushStatus()).toBe("unsupported");
  });

  it("is denied when Notification permission is blocked", async () => {
    stubPushEnv({ permission: "denied" });
    expect(await getPushStatus()).toBe("denied");
  });

  it("is on when a subscription exists, off otherwise", async () => {
    stubPushEnv({ permission: "granted", subscription: makeFakeSubscription() });
    expect(await getPushStatus()).toBe("on");

    stubPushEnv({ permission: "granted", subscription: null });
    expect(await getPushStatus()).toBe("off");
  });
});

// ── enablePush ────────────────────────────────────────────────────

describe("enablePush", () => {
  it("subscribes with the server key and persists the subscription", async () => {
    const { pushManager } = stubPushEnv();

    expect(await enablePush()).toBe("on");

    expect(apiMock.get).toHaveBeenCalledWith("/notifications/vapid-public-key");
    expect(pushManager.subscribe).toHaveBeenCalledTimes(1);
    const arg = pushManager.subscribe.mock.calls[0][0];
    expect(arg.userVisibleOnly).toBe(true);
    expect(Array.from(arg.applicationServerKey)).toEqual(
      Array.from(urlBase64ToUint8Array("QUJDRA"))
    );
    expect(apiMock.post).toHaveBeenCalledWith("/notifications/push-subscribe", {
      endpoint: "https://push.example.com/sub/1",
      keys: { p256dh: "BKey", auth: "Auth" },
      user_agent: expect.any(String),
    });
  });

  it("returns denied without touching the API when permission is refused", async () => {
    const { pushManager } = stubPushEnv({ requestPermissionResult: "denied" });

    expect(await enablePush()).toBe("denied");

    expect(apiMock.get).not.toHaveBeenCalled();
    expect(pushManager.subscribe).not.toHaveBeenCalled();
    expect(apiMock.post).not.toHaveBeenCalled();
  });

  it("returns unconfigured when the server has no VAPID key", async () => {
    const { pushManager } = stubPushEnv();
    apiMock.get.mockResolvedValue({ data: { configured: false, public_key: null } });

    expect(await enablePush()).toBe("unconfigured");
    expect(pushManager.subscribe).not.toHaveBeenCalled();
  });

  it("reuses an existing browser subscription instead of re-subscribing", async () => {
    const existing = makeFakeSubscription("https://push.example.com/sub/existing");
    const { pushManager } = stubPushEnv({ subscription: existing });

    expect(await enablePush()).toBe("on");

    expect(pushManager.subscribe).not.toHaveBeenCalled();
    expect(apiMock.post).toHaveBeenCalledWith(
      "/notifications/push-subscribe",
      expect.objectContaining({ endpoint: "https://push.example.com/sub/existing" })
    );
  });
});

// ── disablePush ───────────────────────────────────────────────────

describe("disablePush", () => {
  it("unsubscribes in the browser and deletes the server row", async () => {
    const sub = makeFakeSubscription();
    stubPushEnv({ subscription: sub });

    expect(await disablePush()).toBe("off");

    expect(sub.unsubscribe).toHaveBeenCalledTimes(1);
    expect(apiMock.delete).toHaveBeenCalledWith("/notifications/push-subscribe", {
      data: { endpoint: "https://push.example.com/sub/1" },
    });
  });

  it("still reports off when the backend delete fails (browser intent wins)", async () => {
    const sub = makeFakeSubscription();
    stubPushEnv({ subscription: sub });
    apiMock.delete.mockRejectedValue(new Error("boom"));

    expect(await disablePush()).toBe("off");
    expect(sub.unsubscribe).toHaveBeenCalledTimes(1);
  });

  it("is a no-op without an active subscription", async () => {
    stubPushEnv({ subscription: null });
    expect(await disablePush()).toBe("off");
    expect(apiMock.delete).not.toHaveBeenCalled();
  });
});

describe("auth session boundary", () => {
  it("unsubscribes locally after logout without calling the authenticated API", async () => {
    const sub = makeFakeSubscription();
    stubPushEnv({ subscription: sub });
    useAuthStore.getState().setAuth("token-a", {
      id: "user-a", email: "a@example.com", role: "viewer",
    });
    const stop = clearPushSubscriptionOnAuthChange();

    useAuthStore.getState().clearAuth();

    await vi.waitFor(() => expect(sub.unsubscribe).toHaveBeenCalledTimes(1));
    expect(apiMock.delete).not.toHaveBeenCalled();
    stop();
  });

  it("unsubscribes when one authenticated account replaces another", async () => {
    const sub = makeFakeSubscription();
    stubPushEnv({ subscription: sub });
    useAuthStore.getState().setAuth("token-a", {
      id: "user-a", email: "a@example.com", role: "viewer",
    });
    const stop = clearPushSubscriptionOnAuthChange();

    useAuthStore.getState().setAuth("token-b", {
      id: "user-b", email: "b@example.com", role: "viewer",
    });

    await vi.waitFor(() => expect(sub.unsubscribe).toHaveBeenCalledTimes(1));
    stop();
  });

  it("preserves push on initial session restore and same-user token rotation", async () => {
    const sub = makeFakeSubscription();
    stubPushEnv({ subscription: sub });
    const stop = clearPushSubscriptionOnAuthChange();

    useAuthStore.getState().setAuth("token-a", {
      id: "user-a", email: "a@example.com", role: "viewer",
    });
    useAuthStore.getState().setToken("token-refreshed");
    await Promise.resolve();

    expect(sub.unsubscribe).not.toHaveBeenCalled();
    stop();
  });

  it("can remove a browser subscription without deleting backend state", async () => {
    const sub = makeFakeSubscription();
    stubPushEnv({ subscription: sub });

    await unsubscribePushLocally();

    expect(sub.unsubscribe).toHaveBeenCalledTimes(1);
    expect(apiMock.delete).not.toHaveBeenCalled();
  });
});
