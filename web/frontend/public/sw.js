/*
 * OfferFlow Service Worker
 *
 * 目标：让「添加到主屏幕」后具备类 App 体验（独立窗口、断网仍能打开外壳），
 * 同时**绝不引入「用户卡在旧版本」的经典问题**。
 *
 * 三条策略：
 *   1. /api/**            → 完全不拦截（业务数据一律走网络，永不缓存）
 *   2. 页面导航            → 网络优先，失败才回退缓存的外壳（离线可用）
 *   3. 带哈希的静态产物     → 缓存优先（文件名变了就是新文件，不存在脏数据）
 *
 * 升级要点：skipWaiting + clients.claim，新版本立即接管，不等待标签页关闭。
 */

const VERSION = "v1";
const SHELL_CACHE = `offerflow-shell-${VERSION}`;
const ASSET_CACHE = `offerflow-assets-${VERSION}`;
const KEEP = [SHELL_CACHE, ASSET_CACHE];

self.addEventListener("install", (event) => {
  event.waitUntil(
    (async () => {
      // 预缓存外壳，保证首次离线也能打开
      const cache = await caches.open(SHELL_CACHE);
      await cache.add(new Request("/", { cache: "reload" })).catch(() => {});
      await self.skipWaiting();
    })(),
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    (async () => {
      const keys = await caches.keys();
      await Promise.all(
        keys.filter((k) => !KEEP.includes(k)).map((k) => caches.delete(k)),
      );
      await self.clients.claim();
    })(),
  );
});

/** 缓存优先：命中直接返回，未命中则取网络并写入缓存 */
async function cacheFirst(request, cacheName) {
  const cache = await caches.open(cacheName);
  const hit = await cache.match(request);
  if (hit) return hit;
  const res = await fetch(request);
  // 只缓存成功的基本响应（避免把 404/206/opaque 塞进缓存）
  if (res && res.ok && res.status === 200 && res.type === "basic") {
    cache.put(request, res.clone());
  }
  return res;
}

/** 网络优先：拿不到网络（离线）时回退缓存的外壳文档 */
async function networkFirst(request) {
  const cache = await caches.open(SHELL_CACHE);
  try {
    const res = await fetch(request);
    if (res && res.ok) {
      // SPA 各路由返回的都是同一份 index.html，统一按 "/" 存一份即可
      cache.put(new Request("/"), res.clone()).catch(() => {});
    }
    return res;
  } catch (err) {
    const cached = await cache.match("/");
    if (cached) return cached;
    throw err;
  }
}

self.addEventListener("fetch", (event) => {
  const req = event.request;

  // 只处理同源 GET
  if (req.method !== "GET") return;
  const url = new URL(req.url);
  if (url.origin !== self.location.origin) return;

  // 业务接口一律直连网络：缓存 API 会导致看到过期数据
  if (url.pathname.startsWith("/api/")) return;

  // 页面导航：网络优先（保证总能拿到最新外壳，进而拿到最新哈希资源）
  if (req.mode === "navigate") {
    event.respondWith(networkFirst(req));
    return;
  }

  // 带内容哈希的产物 + 图标/清单等静态资源：缓存优先
  if (
    url.pathname.startsWith("/assets/") ||
    /\.(png|jpe?g|svg|ico|webmanifest|woff2?|css|js)$/.test(url.pathname)
  ) {
    event.respondWith(cacheFirst(req, ASSET_CACHE));
  }
});
