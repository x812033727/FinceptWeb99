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
 *  - API /api/* (read):      Network-first w/ 3 s timeout. Offline
 *                            fallback only if cached response is < 5 min
 *                            old — financial data going stale silently
 *                            is worse than failing visibly.
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

// Vite emits assets with a content-hash suffix
// (e.g. /assets/index-BVSWP350.js). The filename changes whenever the
// content changes, so a cached entry under a given URL is guaranteed
// to match the current build → safe for cache-first.
const HASHED_ASSET_RE = /^\/assets\/.+-[A-Za-z0-9_-]{8,}\.(js|css)$/;

// API responses older than this are not used as offline fallback.
// Quotes / screener / fundamentals all rotate within minutes; serving
// older data without indication would be worse than failing.
const API_MAX_STALE_MS = 5 * 60 * 1000;

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

  // API routes: network-first with 3s timeout, fallback to cache
  // (only if cache is fresh enough — see API_MAX_STALE_MS).
  if (url.pathname.startsWith("/api/")) {
    event.respondWith(networkFirstWithCache(request, API_CACHE, 3000));
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
