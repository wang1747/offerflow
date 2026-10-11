/**
 * 离线层用的极简 IndexedDB 封装（零第三方依赖）。
 *
 * 为什么不用 localStorage：投递/简历/岗位/面经的列表数据体积会超过 localStorage 的
 * 5MB 上限，而且它是同步 API —— 写一份大 JSON 会直接卡住主线程。
 *
 * 为什么必须自己做缓存而不能靠 Service Worker：线上站点是 IP + HTTP（非安全上下文），
 * Service Worker 只在安全上下文可用，手机上根本注册不上。所以缓存只能由应用层自己来。
 *
 * 降级策略：IndexedDB 不可用（隐私模式、配额被拒等）时退回纯内存 Map ——
 * 功能会退化（关掉页面就丢），但**绝不抛错**，主流程不受影响。
 */

const DB_NAME = "offerflow_offline";
const DB_VERSION = 1;

export const STORE_CACHE = "cache";
export const STORE_QUEUE = "queue";
export const STORE_META = "meta";

/** IndexedDB 不可用时的内存兜底 */
const memory = new Map<string, Map<string, unknown>>();

function memStore(name: string): Map<string, unknown> {
  let m = memory.get(name);
  if (!m) {
    m = new Map<string, unknown>();
    memory.set(name, m);
  }
  return m;
}

function keyOf(value: unknown): string {
  if (typeof value === "string") return value;
  if (value && typeof value === "object") {
    const rec = value as Record<string, unknown>;
    if (rec.key !== undefined) return String(rec.key);
    if (rec.id !== undefined) return String(rec.id);
  }
  return String(value);
}

function promisify<T>(request: IDBRequest<T>): Promise<T> {
  return new Promise<T>((resolve, reject) => {
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error);
  });
}

function txDone(tx: IDBTransaction): Promise<void> {
  return new Promise<void>((resolve, reject) => {
    tx.oncomplete = () => resolve();
    tx.onerror = () => reject(tx.error);
    tx.onabort = () => reject(tx.error);
  });
}

let dbPromise: Promise<IDBDatabase | null> | null = null;

/** 打开（并按需建表）离线库。任何失败都返回 null，调用方据此走内存兜底。 */
export function openDb(): Promise<IDBDatabase | null> {
  if (dbPromise) return dbPromise;
  dbPromise = new Promise<IDBDatabase | null>((resolve) => {
    try {
      if (typeof indexedDB === "undefined") {
        resolve(null);
        return;
      }
      const req = indexedDB.open(DB_NAME, DB_VERSION);
      req.onupgradeneeded = () => {
        const db = req.result;
        // cache：按「请求路径」存最近一次成功的响应体
        if (!db.objectStoreNames.contains(STORE_CACHE)) {
          db.createObjectStore(STORE_CACHE, { keyPath: "key" });
        }
        // queue：离线写队列，自增主键保证重放顺序
        if (!db.objectStoreNames.contains(STORE_QUEUE)) {
          db.createObjectStore(STORE_QUEUE, { keyPath: "id", autoIncrement: true });
        }
        // meta：owner（缓存归属的用户 id）等零散状态
        if (!db.objectStoreNames.contains(STORE_META)) {
          db.createObjectStore(STORE_META, { keyPath: "key" });
        }
      };
      req.onsuccess = () => resolve(req.result);
      req.onerror = () => resolve(null);
      req.onblocked = () => resolve(null);
    } catch {
      resolve(null);
    }
  });
  return dbPromise;
}

export async function idbGet<T>(store: string, key: string): Promise<T | null> {
  const db = await openDb();
  if (!db) {
    return (memStore(store).get(key) as T | undefined) ?? null;
  }
  try {
    const tx = db.transaction(store, "readonly");
    const out = await promisify<T | undefined>(tx.objectStore(store).get(key));
    return out ?? null;
  } catch {
    return null;
  }
}

export async function idbPut(store: string, value: unknown): Promise<void> {
  const db = await openDb();
  if (!db) {
    memStore(store).set(keyOf(value), value);
    return;
  }
  try {
    const tx = db.transaction(store, "readwrite");
    tx.objectStore(store).put(value);
    await txDone(tx);
  } catch {
    // 写失败不影响主流程
  }
}

/** 自增写入（队列专用），返回主键；失败返回 -1 */
export async function idbAdd(store: string, value: unknown): Promise<number> {
  const db = await openDb();
  if (!db) {
    const m = memStore(store);
    let next = 1;
    for (const k of m.keys()) {
      const n = Number(k);
      if (Number.isFinite(n) && n >= next) next = n + 1;
    }
    m.set(String(next), value);
    return next;
  }
  try {
    const tx = db.transaction(store, "readwrite");
    const key = await promisify<IDBValidKey>(tx.objectStore(store).add(value));
    await txDone(tx);
    return typeof key === "number" ? key : -1;
  } catch {
    return -1;
  }
}

export async function idbDelete(store: string, key: string | number): Promise<void> {
  const db = await openDb();
  if (!db) {
    memStore(store).delete(String(key));
    return;
  }
  try {
    const tx = db.transaction(store, "readwrite");
    tx.objectStore(store).delete(key);
    await txDone(tx);
  } catch {
    // 忽略
  }
}

export async function idbAll<T>(store: string): Promise<T[]> {
  const db = await openDb();
  if (!db) {
    return Array.from(memStore(store).values()) as T[];
  }
  try {
    const tx = db.transaction(store, "readonly");
    const out = await promisify<T[]>(tx.objectStore(store).getAll());
    return out ?? [];
  } catch {
    return [];
  }
}

export async function idbClear(store: string): Promise<void> {
  const db = await openDb();
  if (!db) {
    memStore(store).clear();
    return;
  }
  try {
    const tx = db.transaction(store, "readwrite");
    tx.objectStore(store).clear();
    await txDone(tx);
  } catch {
    // 忽略
  }
}
