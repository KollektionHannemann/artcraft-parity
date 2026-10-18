/* ArtCraft Tracker service worker.

   Page and data: network first, so an online visit always shows the newest numbers. The last good copy is
   kept for offline use, and a copy served from the cache carries "x-tracker-cache: offline", so the page
   says it is showing saved data instead of passing it off as current.
   Icons and the manifest: cache first. Requests to other origins (GitHub API, fonts, analytics) are not
   touched. Bump VERSION when the list of shell files changes. */
const VERSION = "tracker-v2";
const SHELL = ["./", "index.html", "manifest.webmanifest", "icons/favicon.svg", "icons/favicon-32.png",
  "icons/icon-192.png", "icons/icon-512.png", "icons/apple-touch-icon.png"];
const NETWORK_TIMEOUT = 8000;

self.addEventListener("install", (event) => {
  event.waitUntil(caches.open(VERSION).then((c) => c.addAll(SHELL)).then(() => self.skipWaiting()));
});

self.addEventListener("activate", (event) => {
  event.waitUntil(caches.keys()
    .then((keys) => Promise.all(keys.filter((k) => k !== VERSION).map((k) => caches.delete(k))))
    .then(() => self.clients.claim()));
});

function markOffline(res) {
  const headers = new Headers(res.headers);
  headers.set("x-tracker-cache", "offline");
  return res.blob().then((body) => new Response(body, { status: res.status, statusText: res.statusText, headers }));
}

function withTimeout(promise, ms) {
  return new Promise((resolve, reject) => {
    const t = setTimeout(() => reject(new Error("timeout")), ms);
    promise.then((v) => { clearTimeout(t); resolve(v); }, (e) => { clearTimeout(t); reject(e); });
  });
}

/* A response is only stored if it is what the file should be: a captive portal answering data/overview.json
   with an HTML login page must not replace the saved data. */
function usable(res, key) {
  if (!res.ok) return false;
  const type = res.headers.get("content-type") || "";
  return key.endsWith(".json") ? type.includes("json") : true;
}

async function networkFirst(request, cacheKey) {
  const cache = await caches.open(VERSION);
  const key = cacheKey || new URL(request.url).pathname;  // one entry per file, whatever the query string
  let res = null;
  try {
    res = await withTimeout(fetch(request), NETWORK_TIMEOUT);
  } catch (err) {
    res = null;  // offline or too slow
  }
  if (res && usable(res, key)) {
    cache.put(key, res.clone()).catch(() => {});  // a full or blocked cache must not discard a good response
    return res;
  }
  if (!res || res.status >= 500 || res.ok) {  // offline, server error or unusable content: try the saved copy
    const hit = await cache.match(key, { ignoreSearch: true });
    if (hit) return markOffline(hit);
  }
  if (res) return res;
  throw new Error("offline and not saved");
}

async function cacheFirst(request) {
  const cache = await caches.open(VERSION);
  const hit = await cache.match(request, { ignoreSearch: true });
  if (hit) return hit;
  const res = await fetch(request);
  if (res.ok) cache.put(request, res.clone());
  return res;
}

self.addEventListener("fetch", (event) => {
  const req = event.request;
  if (req.method !== "GET") return;
  const url = new URL(req.url);
  if (url.origin !== self.location.origin) return;
  if (req.mode === "navigate") {
    event.respondWith(networkFirst(req, new URL("index.html", self.registration.scope).pathname));
  } else if (/\.(json|html)$/.test(url.pathname) || url.pathname.endsWith("/")) {
    event.respondWith(networkFirst(req));
  } else if (url.pathname.includes("/icons/") || url.pathname.endsWith(".webmanifest")) {
    event.respondWith(cacheFirst(req));
  }
});
