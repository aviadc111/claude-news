/* Claude News service worker: offline shell + push notifications. */
const CACHE = "claude-news-v14";
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

// Every message deletes itself 48 hours after it was sent: from the app's store, and from
// the phone's notification list where the phone allows it.
const KEEP_MS = 48 * 3600 * 1000;
async function pruneOld() {
  const cutoff = Date.now() - KEEP_MS;
  try {
    const db = await openDB();
    await new Promise((resolve) => {
      const tx = db.transaction("messages", "readwrite");
      const store = tx.objectStore("messages");
      const req = store.getAll();
      req.onsuccess = () => req.result.forEach((m) => { if (m.time < cutoff) store.delete(m.id); });
      tx.oncomplete = resolve; tx.onerror = resolve;
    });
  } catch (e) { /* storage unavailable */ }
  try {
    const shown = await self.registration.getNotifications();
    shown.forEach((n) => { const t = (n.data && n.data.time) || n.timestamp; if (t && t < cutoff) n.close(); });
  } catch (e) { /* not supported here */ }
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
      // the app page is one page whatever follows the "?" (a tapped notification adds ?tab=...)
      const key = event.request.mode === "navigate" ? url.origin + url.pathname : event.request;
      const cached = await cache.match(key);
      const req = isPage ? new Request(url.href, { cache: "no-cache", credentials: "same-origin" }) : event.request;
      const network = fetch(req).then((res) => {
        if (res && res.ok) cache.put(key, res.clone());
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
      data: { url: msg.url, project: msg.project, time: msg.time },
    });
    await pruneOld();
    const clients = await self.clients.matchAll({ type: "window", includeUncontrolled: true });
    clients.forEach((c) => c.postMessage({ type: "new-message", message: msg }));
  })());
});

// A tapped notification opens the app on the category (tab) its message belongs to.
// The category reaches the page three ways, because phones differ in which one arrives:
// a note the page reads when it comes to the front, a direct message to an open page,
// and ?tab=... on the address when the app has to be started.
async function noteOpenTab(project) {
  try {
    const cache = await caches.open(CACHE);
    await cache.put(new URL("__open-tab", self.registration.scope).href,
      new Response(JSON.stringify({ project, time: Date.now() }), { headers: { "Content-Type": "application/json" } }));
  } catch (e) { /* storage unavailable: the other two ways still work */ }
}
self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  const d = event.notification.data || {};
  const project = d.project || "";
  const scope = self.registration.scope;
  const target = d.url || (project ? scope + "?tab=" + encodeURIComponent(project) : scope);
  event.waitUntil((async () => {
    if (project) await noteOpenTab(project);
    const clients = await self.clients.matchAll({ type: "window", includeUncontrolled: true });
    for (const c of clients) {
      if ("focus" in c) {
        if (project) c.postMessage({ type: "open-tab", project });
        try { await c.focus(); } catch (e) { /* the phone brings the app forward itself */ }
        return;
      }
    }
    await self.clients.openWindow(target);
  })());
});
