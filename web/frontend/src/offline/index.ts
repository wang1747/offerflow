/**
 * 网页端离线层。
 *
 * 目标（对齐电脑版的本地主存 + 双向同步，只是实现换成浏览器里的 IndexedDB）：
 *   1. 断网也能看：所有 /api 读接口的最近一次成功结果落 IndexedDB，断网时回退到它。
 *   2. 断网也能改：不需要立刻调大模型的写操作入队，本地列表先乐观更新。
 *   3. 联网自动同步：网络恢复、定时、启动时都会尝试重放队列，成功后刷新受影响的列表。
 *
 * 边界（必须如实）：
 *   - 只在**网络层失败**（fetch 抛错）时才走离线路径。HTTP 401/403/400 这类业务失败
 *     一律照常抛给调用方 —— 否则会把"登录过期"伪装成"离线"，用户会看到过期数据还以为正常。
 *   - 需要服务器调大模型的操作（生成、解析、抽题、每轮谈判）**不排队**，离线时明确报错。
 *   - 缓存按登录令牌归属：换了账号（令牌变了）就整库清掉，避免 A 的数据被 B 看到。
 */

import {
  idbAdd,
  idbAll,
  idbClear,
  idbDelete,
  idbGet,
  idbPut,
  STORE_CACHE,
  STORE_META,
  STORE_QUEUE,
} from "./db";
import {
  idFromPath,
  isCacheableRead,
  matchQueueRule,
  newLocalId,
  type WriteCtx,
} from "./policy";

/** 有待同步数据时的重试间隔 */
const RETRY_INTERVAL_MS = 20_000;
/** 与 api/client.ts 保持一致：VITE_API_URL 为空时走同源 */
const BASE_URL = (import.meta.env.VITE_API_URL as string | undefined) || "";

const META_OWNER = "owner";
const SYNCED_EVENT = "offerflow:synced";

export interface OfflineStatus {
  /** 浏览器报告的联网状态 */
  online: boolean;
  /** 待同步条数 */
  pending: number;
  /** 正在同步 */
  syncing: boolean;
  /** 上次同步成功时间 */
  lastSyncAt: number | null;
  /** 上次同步失败原因（人话） */
  lastError: string | null;
  /** 当前页面数据来自哪一刻的缓存（null = 实时数据） */
  staleAt: number | null;
}

interface CacheRow {
  key: string;
  at: number;
  body: string;
}

interface QueueRow {
  id?: number;
  method: string;
  path: string;
  body: string | null;
  at: number;
  label: string;
  listKey: string;
}

let state: OfflineStatus = {
  online: typeof navigator === "undefined" ? true : navigator.onLine,
  pending: 0,
  syncing: false,
  lastSyncAt: null,
  lastError: null,
  staleAt: null,
};

const listeners = new Set<(s: OfflineStatus) => void>();

function emit(): void {
  const snapshot: OfflineStatus = { ...state };
  listeners.forEach((fn) => {
    try {
      fn(snapshot);
    } catch {
      // 订阅者自己的异常不能影响离线层
    }
  });
}

export function getStatus(): OfflineStatus {
  return { ...state };
}

export function subscribe(fn: (s: OfflineStatus) => void): () => void {
  listeners.add(fn);
  fn(getStatus());
  return () => listeners.delete(fn);
}

// ---------------------------------------------------------------- 归属

/**
 * 用登录令牌派生归属键。
 * 不直接用用户 id：离线冷启动时还没有 me 的返回，拿不到 id，而令牌就在 localStorage 里。
 */
function ownerKey(): string {
  try {
    const token = localStorage.getItem("access_token") ?? "";
    if (!token) return "";
    let h = 5381;
    for (let i = 0; i < token.length; i += 1) {
      h = ((h * 33) ^ token.charCodeAt(i)) >>> 0;
    }
    return h.toString(36);
  } catch {
    return "";
  }
}

/** 令牌变了就整库清掉（换账号绝不能让上一个人的数据露出来） */
async function ensureOwner(): Promise<void> {
  const key = ownerKey();
  if (!key) return;
  const row = await idbGet<{ key: string; value: string }>(STORE_META, META_OWNER);
  if (row && row.value && row.value !== key) {
    await idbClear(STORE_CACHE);
    await idbClear(STORE_QUEUE);
  }
  if (!row || row.value !== key) {
    await idbPut(STORE_META, { key: META_OWNER, value: key });
  }
}

function scoped(path: string): string {
  const key = ownerKey();
  return key ? `${key}::${path}` : `anon::${path}`;
}

// ---------------------------------------------------------------- 读缓存

export async function readCache(path: string): Promise<{ body: string; at: number } | null> {
  if (!isCacheableRead(path)) return null;
  const row = await idbGet<CacheRow>(STORE_CACHE, scoped(path));
  if (!row || typeof row.body !== "string") return null;
  return { body: row.body, at: row.at };
}

export async function writeCache(path: string, body: string): Promise<void> {
  if (!isCacheableRead(path)) return;
  try {
    await idbPut(STORE_CACHE, { key: scoped(path), at: Date.now(), body });
  } catch {
    // 写缓存失败不影响主流程
  }
}

/** 由 api 层在"网络失败、回退到缓存"时调用，让界面能提示数据是旧的 */
export function markStale(at: number): void {
  if (state.staleAt === at) return;
  state.staleAt = at;
  emit();
}

/** 由 api 层在"成功拿到实时数据"时调用 */
export function markFresh(): void {
  if (state.staleAt === null) return;
  state.staleAt = null;
  emit();
}

// ---------------------------------------------------------------- 写队列

export interface EnqueueInput {
  method: string;
  path: string;
  body: string | null;
  /** 这次写作用在哪个对象上（删除/更新用），从路径里解析 */
  targetId?: string;
}

export interface EnqueueResult {
  /** 本地临时 id（新建时给界面占位用） */
  tempId: string;
  label: string;
}

/**
 * 把一次写操作排进队列，并把结果乐观反映到本地列表缓存上。
 * 返回 null 表示这类操作不支持离线（调用方应抛出"需要联网"）。
 */
export async function enqueueWrite(input: EnqueueInput): Promise<EnqueueResult | null> {
  const rule = matchQueueRule(input.method, input.path);
  if (!rule) return null;

  const ctx: WriteCtx = {
    tempId: newLocalId(),
    targetId: input.targetId || idFromPath(input.path),
  };

  // 新建时把客户端 id 一并写进请求体：服务端对同 id 幂等，
  // 避免"请求其实到了服务器但响应丢了"导致重放时多建一条。
  let body = input.body;
  if (rule.method === "POST" && body) {
    try {
      const parsed = JSON.parse(body) as Record<string, unknown>;
      if (parsed.id === undefined) {
        parsed.id = ctx.tempId;
        body = JSON.stringify(parsed);
      }
    } catch {
      // 请求体不是 JSON 就原样发
    }
  }

  // 1) 乐观更新本地列表，让用户离线也能看到自己刚做的改动
  if (rule.apply) {
    const cached = await readCache(rule.listKey);
    if (cached) {
      try {
        const envelope = JSON.parse(cached.body) as Record<string, unknown>;
        const nextData = rule.apply(envelope.data, body ? JSON.parse(body) : null, ctx);
        envelope.data = nextData;
        await writeCache(rule.listKey, JSON.stringify(envelope));
      } catch {
        // 缓存体坏了就跳过乐观更新，等联网后刷新
      }
    }
  }

  // 2) 入队
  const row: QueueRow = {
    method: rule.method,
    path: input.path,
    body,
    at: Date.now(),
    label: rule.label,
    listKey: rule.listKey,
  };
  await idbAdd(STORE_QUEUE, row);
  await refreshPending();
  emit();

  return { tempId: ctx.tempId, label: rule.label };
}

export async function refreshPending(): Promise<number> {
  const rows = await idbAll<QueueRow>(STORE_QUEUE);
  state.pending = rows.length;
  return state.pending;
}

/** 取队列里各条的人话标签（界面提示"待同步：新增投递、删除岗位"用） */
export async function pendingLabels(): Promise<string[]> {
  const rows = await idbAll<QueueRow>(STORE_QUEUE);
  return rows.map((r) => r.label);
}

// ---------------------------------------------------------------- 同步

async function rawSend(method: string, path: string, body: string | null): Promise<Response> {
  const headers: Record<string, string> = { "Content-Type": "application/json" };
  try {
    const token = localStorage.getItem("access_token");
    if (token) headers.Authorization = `Bearer ${token}`;
  } catch {
    // localStorage 不可用时按匿名发
  }
  return fetch(`${BASE_URL}${path}`, {
    method,
    headers,
    body: body ?? undefined,
    cache: "no-store",
  });
}

async function refreshList(listKey: string): Promise<void> {
  try {
    const res = await rawSend("GET", listKey, null);
    if (res.ok) {
      const text = await res.text();
      await writeCache(listKey, text);
    }
  } catch {
    // 刷新失败不致命：缓存里已有乐观更新的结果
  }
}

let syncing = false;

/**
 * 重放队列。失败分类处理：
 *   - 网络错误 / 5xx  → 停在这里，等下轮（服务器可能只是临时抽风）
 *   - 401            → 停（登录已失效，继续发只是白发）
 *   - 其他 4xx        → 这条永远成功不了，丢掉落并记原因（比如记录已被别处删掉）
 */
export async function flushQueue(): Promise<{ ok: number; dropped: number }> {
  if (syncing) return { ok: 0, dropped: 0 };
  if (typeof navigator !== "undefined" && !navigator.onLine) return { ok: 0, dropped: 0 };

  syncing = true;
  state.syncing = true;
  state.lastError = null;
  emit();

  let ok = 0;
  let dropped = 0;
  const dirty = new Set<string>();

  try {
    await ensureOwner();
    const rows = (await idbAll<QueueRow>(STORE_QUEUE)).sort(
      (a, b) => (a.id ?? 0) - (b.id ?? 0),
    );

    for (const row of rows) {
      let res: Response;
      try {
        res = await rawSend(row.method, row.path, row.body);
      } catch {
        // 网络仍然不通，保留队列
        break;
      }
      if (res.ok) {
        ok += 1;
        dirty.add(row.listKey);
        if (row.id !== undefined) await idbDelete(STORE_QUEUE, row.id);
        continue;
      }
      if (res.status === 401) {
        state.lastError = "登录状态已失效，请重新登录后再同步";
        break;
      }
      if (res.status >= 400 && res.status < 500) {
        dropped += 1;
        let detail = `HTTP ${res.status}`;
        try {
          const text = await res.text();
          const parsed = JSON.parse(text) as { error?: string; detail?: string };
          detail = parsed.error || parsed.detail || detail;
        } catch {
          // 用状态码兜底
        }
        state.lastError = `${row.label}同步失败：${detail}`;
        if (row.id !== undefined) await idbDelete(STORE_QUEUE, row.id);
        continue;
      }
      // 5xx：服务器问题，保留队列下轮再试
      state.lastError = `${row.label}同步失败：服务器暂时不可用`;
      break;
    }

    for (const key of dirty) {
      await refreshList(key);
    }
    if (dirty.size > 0) {
      try {
        window.dispatchEvent(new CustomEvent(SYNCED_EVENT, { detail: { keys: Array.from(dirty) } }));
      } catch {
        // 无 window（测试环境）就跳过
      }
    }
    if (ok > 0 || dropped > 0) state.lastSyncAt = Date.now();
  } finally {
    syncing = false;
    state.syncing = false;
    await refreshPending();
    emit();
  }

  return { ok, dropped };
}

/** 页面里可以监听这个事件，在同步完成后重新拉一次列表 */
export function onSynced(fn: (keys: string[]) => void): () => void {
  const handler = (e: Event): void => {
    const detail = (e as CustomEvent<{ keys?: string[] }>).detail;
    fn(detail?.keys ?? []);
  };
  window.addEventListener(SYNCED_EVENT, handler);
  return () => window.removeEventListener(SYNCED_EVENT, handler);
}

// ---------------------------------------------------------------- 启动

let inited = false;
let timer: number | null = null;

export function initOffline(): void {
  if (inited) return;
  inited = true;

  state.online = typeof navigator === "undefined" ? true : navigator.onLine;

  window.addEventListener("online", () => {
    state.online = true;
    emit();
    void flushQueue();
  });
  window.addEventListener("offline", () => {
    state.online = false;
    markStale(Date.now());
    emit();
  });

  void (async () => {
    await ensureOwner();
    await refreshPending();
    emit();
    if (state.online && state.pending > 0) void flushQueue();
  })();

  // 有积压就定期重试；没有积压时这个定时器什么都不做
  timer = window.setInterval(() => {
    if (state.pending > 0 && state.online && !state.syncing) void flushQueue();
  }, RETRY_INTERVAL_MS);
}

export function stopOffline(): void {
  if (timer !== null) {
    window.clearInterval(timer);
    timer = null;
  }
}

/** 退出登录时清干净（换账号由 ensureOwner 兜底，这个是显式入口） */
export async function clearOffline(): Promise<void> {
  await idbClear(STORE_CACHE);
  await idbClear(STORE_QUEUE);
  await idbDelete(STORE_META, META_OWNER);
  state.pending = 0;
  state.staleAt = null;
  emit();
}
