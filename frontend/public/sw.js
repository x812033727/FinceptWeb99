/**
 * Fincept Web Terminal — Service Worker
 *
 * Caching strategy:
 *  - App shell (HTML/JS/CSS): Cache-first (stale-while-revalidate)
 *  - API /api/*:              Network-first (1s timeout → stale cache)
 *  - Static assets (/icon-*): Cache-first, long TTL
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

// ── Activate: purge old caches ────────────────────────────────────
self.addEventListener("activate", (event) => {
  const current = new Set([SHELL_CACHE, API_CACHE, STATIC_CACHE]);
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((k) => !current.has(k)).map((k) => caches.delete(k)))
    )
  );
  self.clients.claim();
});

// ── Fetch ─────────────────────────────────────────────────────────
self.addEventListener("fetch", (event) => {
  const { request } = event;
  const url = new URL(request.url);

  // Never intercept WebSocket or non-GET
  if (request.method !== "GET" || url.pathname.startsWith("/ws")) return;

  // API routes: network-first with 3s timeout, fallback to cache
  if (url.pathname.startsWith("/api/")) {
    event.respondWith(networkFirstWithCache(request, API_CACHE, 3000));
    return;
  }

  // Static assets: cache-first
  if (/\.(svg|png|ico|woff2|webmanifest)$/.test(url.pathname)) {
    event.respondWith(cacheFirst(request, STATIC_CACHE));
    return;
  }

  // JS/CSS bundles: stale-while-revalidate
  if (/\.(js|css)$/.test(url.pathname)) {
    event.respondWith(staleWhileRevalidate(request, SHELL_CACHE));
    return;
  }

  // HTML (SPA routes): network-first, fallback to cached /
  event.respondWith(spaFallback(request));
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
      cache.put(request, response.clone());
    }
    return response;
  } catch {
    clearTimeout(timer);
    const cached = await caches.match(request);
    if (cached) return cached;
    return new Response(JSON.stringify({ error: "offline" }), {
      status: 503,
      headers: { "Content-Type": "application/json" },
    });
  }
}

async function spaFallback(request) {
  try {
    const response = await fetch(request);
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
