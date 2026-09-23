// The bell's service worker: the page hangs on the phone and opens even
// with no signal — showing the last state it knew, never a blank screen.
//
// Strategy is NETWORK-FIRST for everything, cache as fallback. The bell's
// state changes several times a day and a stale "quiet" is a lie, so the
// cache is only ever consulted when the network has already failed. That
// also means a deploy is live on the next open — no "please refresh"
// purgatory, no version pinned forever.
const CACHE = "divebell-v1";
const SHELL = ["/", "/index.html", "/manifest.webmanifest",
               "/icons/icon-192.png", "/icons/icon-512.png"];

self.addEventListener("install", (e) => {
  e.waitUntil(
    caches.open(CACHE).then((c) => c.addAll(SHELL)).then(() => self.skipWaiting())
  );
});

self.addEventListener("activate", (e) => {
  e.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (e) => {
  const req = e.request;
  if (req.method !== "GET" || new URL(req.url).origin !== self.location.origin) return;
  e.respondWith(
    fetch(req)
      .then((res) => {
        if (res && res.ok) {
          const copy = res.clone();
          caches.open(CACHE).then((c) => c.put(req, copy)).catch(() => {});
        }
        return res;
      })
      .catch(() =>
        caches.match(req).then((hit) =>
          hit || (req.mode === "navigate" ? caches.match("/index.html") : undefined)
        )
      )
  );
});
