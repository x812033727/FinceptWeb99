/**
 * Fincept Web Terminal — Service Worker
 *
 * Caching strategy:
 *  - Vite hashed bundles (/assets/*-[hash].js|css): Cache-first (immutable —
 *    filename includes a content hash, so a different filename means
 *    different content; safe to never re-fetch).
 *  - Other JS/CSS:           Stale-while-revalidate.
 *  - HTML (SPA routes):      Network-first, fallback to cached "/".
 *  - Static assets:          Cache-first, long TTL.
 *  - Public API /api/* read: Network-first w/ 10 s timeout. Offline
 *                            fallback only if cached response is < 5 min old.
 *  - Authenticated API read: Network-only. Cache Storage keys requests by
 *                            URL, not bearer identity, so sharing this cache
 *                            across sessions could disclose owner-scoped data.
 *  - API /api/auth/*:        Network-only (auth must never be served
 *                            from cache).
 *
 * Install: navigator.serviceWorker.register('/sw.js')
 */

// Version is appended to the registration URL by main.tsx (?v=<pkg.version>)
// so each release scopes its caches separately and the activate handler can
// purge anything that doesn't match the current version.
const SW_VERSION    = new URL(self.location.href).searchParams.get("v") || "dev";
const SHELL_CACHE   = `fincept-shell-${SW_VERSION}`;
const API_CACHE     = `fincept-api-${SW_VERSION}`;
const STATIC_CACHE  = `fincept-static-${SW_VERSION}`;

const SHELL_ASSETS = [
  "/",
  "/dashboard",
  "/manifest.webmanifest",
  "/favicon.svg",
];

// ── Install: pre-cache app shell ──────────────────────────────────
self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(SHELL_CACHE).then((cache) =>
      cache.addAll(SHELL_ASSETS).catch(() => {})
    )
  );
  self.skipWaiting();
});

// ── Activate: purge old caches + enable navigation preload ───────
self.addEventListener("activate", (event) => {
  // API_CACHE is deliberately omitted so this worker upgrade purges legacy
  // entries created before authenticated requests became network-only.
  const current = new Set([SHELL_CACHE, STATIC_CACHE]);
  event.waitUntil(
    Promise.all([
      caches.keys().then((keys) =>
        Promise.all(keys.filter((k) => !current.has(k)).map((k) => caches.delete(k)))
      ),
      // Let the browser start the navigation request in parallel with
      // SW boot — on a cold SW this shaves the startup latency off
      // every hard navigation. Guarded: not all engines support it.
      self.registration.navigationPreload?.enable().catch(() => {}),
    ])
  );
  self.clients.claim();
});

// Vite emits assets with a content-hash suffix
// (e.g. /assets/index-BVSWP350.js). The filename changes whenever the
// content changes, so a cached entry under a given URL is guaranteed
// to match the current build → safe for cache-first.
const HASHED_ASSET_RE = /^\/assets\/.+-[A-Za-z0-9_-]{8,}\.(js|css)$/;

// API responses older than this are not used as offline fallback.
// Quotes / screener / fundamentals all rotate within minutes; serving
// older data without indication would be worse than failing.
const API_MAX_STALE_MS = 5 * 60 * 1000;

// Cap the API cache so a long session browsing many symbols can't grow
// it unboundedly. Cache API keys() returns insertion order, so trimming
// from the front is FIFO — close enough to LRU for a freshness-gated
// fallback cache (entries older than API_MAX_STALE_MS are dead weight
// anyway).
const API_CACHE_MAX_ENTRIES = 100;

async function trimCache(cacheName, maxEntries) {
  try {
    const cache = await caches.open(cacheName);
    const keys = await cache.keys();
    for (let i = 0; i < keys.length - maxEntries; i++) {
      await cache.delete(keys[i]);
    }
  } catch { /* trim is best-effort */ }
}

// ── Fetch ─────────────────────────────────────────────────────────
self.addEventListener("fetch", (event) => {
  const { request } = event;
  const url = new URL(request.url);

  // Never intercept WebSocket or non-GET
  if (request.method !== "GET" || url.pathname.startsWith("/ws")) return;

  // Auth endpoints are never cached. Even GET /api/auth/me must hit
  // the server — a stale response could keep a logged-out user
  // looking authenticated until they navigate.
  if (url.pathname.startsWith("/api/auth/")) {
    return; // let the network handle it
  }

  // API routes: network-first with 10s timeout, fallback to cache
  // (only if cache is fresh enough — see API_MAX_STALE_MS).
  // 10s (vs the original 3s) is sized for slow mobile networks: a
  // cold-cache /api/us/screener legitimately takes 5–7 s on 4G, and
  // the previous 3 s ceiling was timing out → falling back to a
  // cached all-zero response from a previous failed warm cycle.
  if (url.pathname.startsWith("/api/")) {
    // Cache Storage matching does not partition entries by Authorization.
    // Owner-scoped responses must therefore stay entirely outside the shared
    // service-worker cache; returning lets the browser use the network.
    if (request.headers.has("Authorization")) return;
    event.respondWith(networkFirstWithCache(request, API_CACHE, 10000));
    return;
  }

  // Static assets: cache-first
  if (/\.(svg|png|ico|woff2|webmanifest)$/.test(url.pathname)) {
    event.respondWith(cacheFirst(request, STATIC_CACHE));
    return;
  }

  // Vite content-hashed bundles: cache-first (immutable by filename).
  if (HASHED_ASSET_RE.test(url.pathname)) {
    event.respondWith(cacheFirst(request, SHELL_CACHE));
    return;
  }

  // Other JS/CSS (sw.js, dev assets): stale-while-revalidate.
  if (/\.(js|css)$/.test(url.pathname)) {
    event.respondWith(staleWhileRevalidate(request, SHELL_CACHE));
    return;
  }

  // HTML (SPA routes): network-first, fallback to cached /
  event.respondWith(spaFallback(request, event));
});

// ── Strategies ────────────────────────────────────────────────────

async function cacheFirst(request, cacheName) {
  const cached = await caches.match(request);
  if (cached) return cached;
  const response = await fetch(request);
  if (response.ok) {
    const cache = await caches.open(cacheName);
    cache.put(request, response.clone());
  }
  return response;
}

async function staleWhileRevalidate(request, cacheName) {
  const cache = await caches.open(cacheName);
  const cached = await cache.match(request);
  const fetchPromise = fetch(request).then((response) => {
    if (response.ok) cache.put(request, response.clone());
    return response;
  }).catch(() => cached);
  return cached || fetchPromise;
}

async function networkFirstWithCache(request, cacheName, timeoutMs) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const response = await fetch(request, { signal: controller.signal });
    clearTimeout(timer);
    if (response.ok) {
      const cache = await caches.open(cacheName);
      // Stamp the response with the time it was cached so the
      // offline-fallback path can drop it once it goes stale. Stored
      // as a header on a cloned response — the original is returned
      // to the page untouched.
      const stamped = new Response(await response.clone().blob(), {
        status: response.status,
        statusText: response.statusText,
        headers: appendDateHeader(response.headers),
      });
      cache.put(request, stamped);
      // Fire-and-forget: bound the cache without delaying the response.
      trimCache(cacheName, API_CACHE_MAX_ENTRIES);
    }
    return response;
  } catch {
    clearTimeout(timer);
    const cached = await caches.match(request);
    if (cached && !isStale(cached, API_MAX_STALE_MS)) return cached;
    return new Response(JSON.stringify({ error: "offline" }), {
      status: 503,
      headers: { "Content-Type": "application/json" },
    });
  }
}

function appendDateHeader(headers) {
  const out = new Headers(headers);
  out.set("X-SW-Cached-At", String(Date.now()));
  return out;
}

function isStale(response, maxAgeMs) {
  const stamp = response.headers.get("X-SW-Cached-At");
  if (!stamp) return false; // legacy entry — treat as fresh once
  const age = Date.now() - Number(stamp);
  return Number.isFinite(age) && age > maxAgeMs;
}

async function spaFallback(request, event) {
  try {
    // Use the browser's preloaded navigation response when available
    // (started in parallel with SW boot — see the activate handler).
    const preloaded = event && (await event.preloadResponse);
    const response = preloaded || (await fetch(request));
    if (response.ok) {
      const cache = await caches.open(SHELL_CACHE);
      cache.put(request, response.clone());
    }
    return response;
  } catch {
    const cached = await caches.match(request) || await caches.match("/");
    return cached || new Response("Offline", { status: 503 });
  }
}

// ── Web Push (D3 瀏覽器推播) ──────────────────────────────────────
// Payload contract (backend web_push_service._notification_payload):
//   { title, body, tag, url }
// `tag` dedupes re-fires of the same repeating alert; `url` is where a
// click should land (defaults to the alerts page).

self.addEventListener("push", (event) => {
  let data = {};
  try {
    data = event.data ? event.data.json() : {};
  } catch {
    // Non-JSON payload (e.g. push-service test ping) — show as body text.
    data = { body: event.data ? event.data.text() : "" };
  }
  const title = data.title || "Fincept";
  event.waitUntil(
    self.registration.showNotification(title, {
      body: data.body || "",
      tag: data.tag || undefined,
      icon: "/icon-512.svg",
      badge: "/favicon.svg",
      data: { url: data.url || "/alerts" },
    })
  );
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  const url = (event.notification.data && event.notification.data.url) || "/";
  event.waitUntil(
    self.clients
      .matchAll({ type: "window", includeUncontrolled: true })
      .then((clientList) => {
        // Prefer an existing tab: navigate it to the target and focus.
        for (const client of clientList) {
          if ("focus" in client) {
            if (client.navigate) client.navigate(url).catch(() => {});
            return client.focus();
          }
        }
        return self.clients.openWindow(url);
      })
  );
});
