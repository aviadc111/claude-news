/* Claude News service worker: offline shell + push notifications. */
const CACHE = "claude-news-v10";
const SHELL = ["./", "./index.html", "./config.js", "./manifest.webmanifest",
               "./icons/icon-192.png", "./icons/icon-512.png", "./icons/logo.svg"];

// ---- tiny IndexedDB store shared with the page ---------------------------
function openDB() {
  return new Promise((resolve, reject) => {
    const req = indexedDB.open("claude-news", 1);
    req.onupgradeneeded = () => {
      const db = req.result;
      if (!db.objectStoreNames.contains("messages")) {
        db.createObjectStore("messages", { keyPath: "id" });
      }
    };
    req.onsuccess = () => resolve(req.result);
    req.onerror = () => reject(req.error);
  });
}
async function storeMessage(msg) {
  try {
    const db = await openDB();
    await new Promise((resolve, reject) => {
      const tx = db.transaction("messages", "readwrite");
      tx.objectStore("messages").put(msg);
      tx.oncomplete = resolve;
      tx.onerror = () => reject(tx.error);
    });
  } catch (e) { /* storage unavailable - the notification still shows */ }
}

// ---- lifecycle ------------------------------------------------------------
self.addEventListener("install", (event) => {
  event.waitUntil(caches.open(CACHE).then((c) => c.addAll(SHELL)).then(() => self.skipWaiting()));
});

self.addEventListener("activate", (event) => {
  event.waitUntil((async () => {
    const keys = await caches.keys();
    await Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k)));
    await self.clients.claim();
    const clients = await self.clients.matchAll({ type: "window", includeUncontrolled: true });
    clients.forEach((c) => c.postMessage({ type: "updated", cache: CACHE }));
  })());
});

// The page itself: network first so a new version shows up at once, cache when offline.
// Other same-origin files: cache first, refreshed in the background. Everything else untouched.
self.addEventListener("fetch", (event) => {
  const url = new URL(event.request.url);
  if (url.origin !== self.location.origin || event.request.method !== "GET") return;
  const isPage = event.request.mode === "navigate" || url.pathname.endsWith("/") || url.pathname.endsWith("index.html")
              || (url.pathname.includes("/data/") && !url.pathname.endsWith("tickers.txt"));   // live data: network first; the big ticker list is fine from cache
  event.respondWith(
    caches.open(CACHE).then(async (cache) => {
      const cached = await cache.match(event.request);
      const req = isPage ? new Request(url.href, { cache: "no-cache", credentials: "same-origin" }) : event.request;
      const network = fetch(req).then((res) => {
        if (res && res.ok) cache.put(event.request, res.clone());
        return res;
      }).catch(() => cached);
      return isPage ? network : (cached || network);
    })
  );
});

// ---- push -----------------------------------------------------------------
self.addEventListener("push", (event) => {
  let data = {};
  try { data = event.data ? event.data.json() : {}; }
  catch (e) { data = { title: "Claude News", body: event.data ? event.data.text() : "" }; }
  const time = data.time ? new Date(data.time).getTime() : Date.now();
  const msg = {
    id: "push-" + time + "-" + Math.random().toString(36).slice(2, 8),
    time, title: data.title || "Claude News", body: data.body || "",
    project: data.project || "General", url: data.url || "",
  };
  event.waitUntil((async () => {
    await storeMessage(msg);
    await self.registration.showNotification(msg.title, {
      body: msg.body,
      icon: "./icons/icon-192.png",
      badge: "./icons/icon-192.png",
      tag: msg.id,
      data: { url: msg.url, project: msg.project },
    });
    const clients = await self.clients.matchAll({ type: "window", includeUncontrolled: true });
    clients.forEach((c) => c.postMessage({ type: "new-message", message: msg }));
  })());
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  const target = (event.notification.data && event.notification.data.url) || self.registration.scope;
  event.waitUntil((async () => {
    const clients = await self.clients.matchAll({ type: "window", includeUncontrolled: true });
    for (const c of clients) {
      if ("focus" in c) { await c.focus(); return; }
    }
    await self.clients.openWindow(target);
  })());
});
