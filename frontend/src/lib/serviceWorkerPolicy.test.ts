import { readFileSync } from "node:fs";
import path from "node:path";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

type WorkerEvent = Record<string, unknown>;
type Listener = (event: WorkerEvent) => void;

const source = readFileSync(path.resolve(process.cwd(), "public/sw.js"), "utf8");

function loadWorker() {
  const listeners: Record<string, Listener> = {};
  const cache = {
    addAll: vi.fn().mockResolvedValue(undefined),
    delete: vi.fn().mockResolvedValue(true),
    keys: vi.fn().mockResolvedValue([]),
    match: vi.fn().mockResolvedValue(undefined),
    put: vi.fn().mockResolvedValue(undefined),
  };
  const caches = {
    delete: vi.fn().mockResolvedValue(true),
    keys: vi.fn().mockResolvedValue([]),
    match: vi.fn().mockResolvedValue(undefined),
    open: vi.fn().mockResolvedValue(cache),
  };
  const scope = {
    location: { href: "https://fincept.test/sw.js?v=test" },
    registration: {},
    clients: { claim: vi.fn(), matchAll: vi.fn() },
    skipWaiting: vi.fn(),
    addEventListener: vi.fn((name: string, listener: Listener) => {
      listeners[name] = listener;
    }),
  };

  new Function("self", "caches", source)(scope, caches);
  return { listeners, caches };
}

function apiRequest(authorization?: string) {
  const headers = new Headers();
  if (authorization) headers.set("Authorization", authorization);
  return {
    method: "GET",
    url: "https://fincept.test/api/portfolio",
    headers,
  };
}

beforeEach(() => {
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue(
    new Response(JSON.stringify({ ok: true }), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    }),
  ));
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("service worker API cache policy", () => {
  it("leaves authenticated API reads on the network without cache fallback", () => {
    const { listeners, caches } = loadWorker();
    const respondWith = vi.fn();

    listeners.fetch({
      request: apiRequest("Bearer user-a-token"),
      respondWith,
    });

    expect(respondWith).not.toHaveBeenCalled();
    expect(caches.open).not.toHaveBeenCalled();
  });

  it("retains the bounded offline cache for unauthenticated public API reads", async () => {
    const { listeners } = loadWorker();
    let responsePromise: Promise<Response> | undefined;

    listeners.fetch({
      request: apiRequest(),
      respondWith: (value: unknown) => {
        responsePromise = value as Promise<Response>;
      },
    });

    expect(responsePromise).toBeDefined();
    await expect(responsePromise).resolves.toBeInstanceOf(Response);
  });

  it("purges legacy API entries when the updated worker activates", async () => {
    const { listeners, caches } = loadWorker();
    caches.keys.mockResolvedValue([
      "fincept-shell-test",
      "fincept-api-test",
      "fincept-static-test",
      "fincept-api-old",
    ]);
    let activation: Promise<unknown> | undefined;

    listeners.activate({
      waitUntil: (value: unknown) => {
        activation = value as Promise<unknown>;
      },
    });
    await activation;

    expect(caches.delete).toHaveBeenCalledWith("fincept-api-test");
    expect(caches.delete).toHaveBeenCalledWith("fincept-api-old");
    expect(caches.delete).not.toHaveBeenCalledWith("fincept-shell-test");
    expect(caches.delete).not.toHaveBeenCalledWith("fincept-static-test");
  });
});
