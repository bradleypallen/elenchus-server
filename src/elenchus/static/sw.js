const CACHE_NAME = "elenchus-v1";

const SHELL_URLS = [
  "/",
  "/static/manifest.json",
  "/static/icon.svg",
  "/static/icon-192.png",
  "/static/icon-512.png",
  "https://cdnjs.cloudflare.com/ajax/libs/react/18.2.0/umd/react.production.min.js",
  "https://cdnjs.cloudflare.com/ajax/libs/react-dom/18.2.0/umd/react-dom.production.min.js",
  "https://cdnjs.cloudflare.com/ajax/libs/babel-standalone/7.23.9/babel.min.js",
  "https://cdnjs.cloudflare.com/ajax/libs/marked/12.0.1/marked.min.js",
];

// Install: pre-cache the app shell
self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME).then((cache) => cache.addAll(SHELL_URLS))
  );
  self.skipWaiting();
});

// Activate: clean up old caches
self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((k) => k !== CACHE_NAME).map((k) => caches.delete(k)))
    )
  );
  self.clients.claim();
});

// Fetch: network-first for API, cache-first for shell
self.addEventListener("fetch", (event) => {
  const url = new URL(event.request.url);

  // API calls: always go to network
  if (url.pathname.startsWith("/api/")) {
    event.respondWith(
      fetch(event.request).catch(() =>
        new Response(JSON.stringify({ error: "Server unreachable" }), {
          status: 503,
          headers: { "Content-Type": "application/json" },
        })
      )
    );
    return;
  }

  // Everything else: try cache first, then network, then offline fallback
  event.respondWith(
    caches.match(event.request).then((cached) => {
      if (cached) return cached;
      return fetch(event.request)
        .then((response) => {
          // Cache successful responses for shell resources
          if (response.ok && (url.origin === self.location.origin || url.origin.includes("cdnjs"))) {
            const clone = response.clone();
            caches.open(CACHE_NAME).then((cache) => cache.put(event.request, clone));
          }
          return response;
        })
        .catch(() => {
          // Offline fallback for navigation requests
          if (event.request.mode === "navigate") {
            return new Response(OFFLINE_HTML, {
              status: 503,
              headers: { "Content-Type": "text/html" },
            });
          }
          return new Response("", { status: 503 });
        });
    })
  );
});

const OFFLINE_HTML = `<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>Elenchus — Offline</title>
<style>
  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
  body {
    font-family: 'IBM Plex Mono', monospace;
    background: #0a0a12; color: #e0e0f0;
    display: flex; align-items: center; justify-content: center;
    min-height: 100vh; padding: 2rem;
  }
  .card {
    max-width: 420px; text-align: center;
    border: 1px solid #2a2a42; border-radius: 8px;
    padding: 2.5rem 2rem; background: #12121e;
  }
  .icon { font-size: 4rem; margin-bottom: 1rem; color: #818cf8; }
  h1 { font-size: 1.2rem; font-weight: 500; margin-bottom: 0.8rem; }
  p { font-size: 0.85rem; color: #a0a0b8; line-height: 1.6; margin-bottom: 1rem; }
  code { color: #818cf8; background: rgba(129,140,248,0.1); padding: 0.15em 0.4em; border-radius: 3px; }
  button {
    margin-top: 0.5rem; padding: 0.6rem 1.5rem;
    background: transparent; color: #818cf8;
    border: 1px solid #818cf8; border-radius: 4px;
    font-family: inherit; font-size: 0.85rem; cursor: pointer;
  }
  button:hover { background: rgba(129,140,248,0.1); }
</style>
</head>
<body>
<div class="card">
  <div class="icon">\u0395</div>
  <h1>Server not reachable</h1>
  <p>The Elenchus server is not running or this device is offline.</p>
  <p>Start the server with <code>elenchus</code> and try again.</p>
  <button onclick="location.reload()">Retry</button>
</div>
</body>
</html>`;
