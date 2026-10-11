/**
 * 离线策略：哪些读要缓存、哪些写可以排队、排队时怎么"乐观更新"本地列表。
 *
 * 原则（与电脑版 P4 一致）：
 *   1. 读全部可缓存 —— 断网时页面还能翻。
 *   2. 写只排队**不需要立刻调大模型**的那些。生成、解析、抽题、谈判每一轮都要服务器
 *      调模型，离线排队没有意义（排到队里也只是延迟失败），一律明确提示"需要联网"。
 *   3. 排队的写必须能**乐观反映到本地列表**，否则用户离线新增了一条、列表里却看不见，
 *      会以为没存上。
 */

/** 写入上下文：tempId 是本地临时 id，targetId 是这次写作用在哪个对象上（可为空） */
export interface WriteCtx {
  tempId: string;
  targetId: string;
}

export interface QueueRule {
  method: "POST" | "PUT" | "PATCH" | "DELETE";
  /** 精确路径或正则 */
  match: string | RegExp;
  /** 人话标签，界面提示用 */
  label: string;
  /** 需要同步刷新的读缓存键（GET 路径） */
  listKey: string;
  /**
   * 乐观更新：把这次写应用到缓存里的 data 上。
   * `data` 是该 GET 接口响应体里的 data（可能是数组，也可能是 {items,total}）。
   */
  apply?: (data: unknown, body: unknown, ctx: WriteCtx) => unknown;
}

/** 生成一个本地临时 id。非安全上下文下 crypto.randomUUID 不存在，必须留退路。 */
export function newLocalId(): string {
  const c = globalThis.crypto as Crypto | undefined;
  if (c && typeof c.randomUUID === "function") {
    try {
      return c.randomUUID();
    } catch {
      // HTTP 页面（WebView）取不到 randomUUID，落到下面的退路
    }
  }
  const seg = (): string => Math.floor(Math.random() * 0x10000).toString(16).padStart(4, "0");
  return `${Date.now().toString(36)}-${seg()}${seg()}-${seg()}`;
}

function asRows(data: unknown): Record<string, unknown>[] {
  return Array.isArray(data) ? (data as Record<string, unknown>[]) : [];
}

/** 按 id 改或删一行；没命中就原样返回（可能是分页的下一页，或已被别的操作删掉） */
function patchById(data: unknown, id: string, patch: Record<string, unknown> | null): unknown {
  if (!Array.isArray(data) || !id) return data;
  const rows = asRows(data);
  if (patch === null) {
    return rows.filter((it) => String(it.id) !== id);
  }
  let hit = false;
  const next = rows.map((it) => {
    if (String(it.id) !== id) return it;
    hit = true;
    return { ...it, ...patch };
  });
  return hit ? next : rows;
}

/** 列表接口的 data 可能是数组，也可能是 {items,total}（面经/题库走这种） */
function removeFromEither(data: unknown, id: string): unknown {
  if (Array.isArray(data)) {
    return asRows(data).filter((it) => String(it.id) !== id);
  }
  if (data && typeof data === "object") {
    const obj = data as Record<string, unknown>;
    if (Array.isArray(obj.items)) {
      const items = (obj.items as Record<string, unknown>[]).filter(
        (it) => String(it.id) !== id,
      );
      const total = typeof obj.total === "number" ? Math.max(0, obj.total - 1) : items.length;
      return { ...obj, items, total };
    }
  }
  return data;
}

export const APP_LIST = "/api/v1/applications";
export const JD_LIST = "/api/v1/jd/list";
export const RESUME_LIST = "/api/v1/resume/list";
export const ARTICLE_LIST = "/api/v1/interview/articles";

export const QUEUE_RULES: QueueRule[] = [
  {
    method: "POST",
    match: APP_LIST,
    label: "新增投递",
    listKey: APP_LIST,
    apply: (data, body, ctx) => {
      const b = (body ?? {}) as Record<string, unknown>;
      const now = new Date().toISOString();
      const row: Record<string, unknown> = {
        id: ctx.tempId,
        company: b.company ?? "",
        position: b.position ?? "",
        // 必须与服务端新建时的默认状态一致，否则同步成功后卡片会"跳列"
        // （离线排在「已投递」、同步后变成「已收藏」，用户会以为状态被改了）。
        status: "saved",
        created_at: now,
        updated_at: now,
      };
      if (b.jd_id) row.jd_id = b.jd_id;
      if (b.resume_id) row.resume_id = b.resume_id;
      return [...asRows(data), row];
    },
  },
  {
    method: "PUT",
    match: /^\/api\/v1\/applications\/[^/]+\/status$/,
    label: "更新投递状态",
    listKey: APP_LIST,
    apply: (data, body, ctx) => {
      const b = (body ?? {}) as Record<string, unknown>;
      return patchById(data, ctx.targetId, { status: b.status });
    },
  },
  {
    method: "PUT",
    match: /^\/api\/v1\/applications\/[^/]+$/,
    label: "保存投递详情",
    listKey: APP_LIST,
    apply: (data, body, ctx) => {
      const b = (body ?? {}) as Record<string, unknown>;
      return patchById(data, ctx.targetId, { ...b });
    },
  },
  {
    method: "DELETE",
    match: /^\/api\/v1\/applications\/[^/]+$/,
    label: "删除投递",
    listKey: APP_LIST,
    apply: (data, _body, ctx) => patchById(data, ctx.targetId, null),
  },
  {
    method: "DELETE",
    match: /^\/api\/v1\/jd\/[^/]+$/,
    label: "删除岗位",
    listKey: JD_LIST,
    apply: (data, _body, ctx) => patchById(data, ctx.targetId, null),
  },
  {
    method: "PUT",
    match: /^\/api\/v1\/jd\/[^/]+$/,
    label: "更新岗位",
    listKey: JD_LIST,
    apply: (data, body, ctx) => {
      const b = (body ?? {}) as Record<string, unknown>;
      return patchById(data, ctx.targetId, { ...b });
    },
  },
  {
    method: "DELETE",
    match: /^\/api\/v1\/resume\/[^/]+$/,
    label: "删除简历",
    listKey: RESUME_LIST,
    apply: (data, _body, ctx) => patchById(data, ctx.targetId, null),
  },
  {
    method: "DELETE",
    match: /^\/api\/v1\/interview\/articles\/[^/]+$/,
    label: "删除面经",
    listKey: ARTICLE_LIST,
    apply: (data, _body, ctx) => removeFromEither(data, ctx.targetId),
  },
];

/** 找到匹配的排队规则；没有匹配说明该写操作必须联网 */
export function matchQueueRule(method: string, path: string): QueueRule | null {
  const m = method.toUpperCase();
  for (const rule of QUEUE_RULES) {
    if (rule.method !== m) continue;
    if (typeof rule.match === "string" ? rule.match === path : rule.match.test(path)) {
      return rule;
    }
  }
  return null;
}

/** 从 "/api/v1/applications/abc/status" 里取出 "abc" */
export function idFromPath(path: string): string {
  const parts = path.split("?")[0].split("/").filter(Boolean);
  // 形如 api / v1 / applications / <id> [/status]
  for (let i = 0; i < parts.length; i += 1) {
    if (["applications", "jd", "resume", "articles"].includes(parts[i]) && parts[i + 1]) {
      return parts[i + 1] === "list" || parts[i + 1] === "all" ? "" : parts[i + 1];
    }
  }
  return "";
}

/** 读缓存：/api/ 下的 GET 都值得缓存；导出类（blob）不走这里 */
export function isCacheableRead(path: string): boolean {
  if (!path.startsWith("/api/")) return false;
  if (path.includes("/export")) return false;
  return true;
}
