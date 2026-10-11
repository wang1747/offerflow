import { useCallback } from "react";
import { useNavigate } from "react-router-dom";
import type { AgentEntryContext } from "../types";

/**
 * 业务页面进入 Agent 的**唯一入口**（设计文档 2.4 / 2.8）。
 *
 * 为什么要有这一层，而不是让每个页面自己 `navigate("/agent?...")`：
 *
 * 1. **业务页面不许直接调 Agent 后端接口**（2.8 原话）。它们只负责"把上下文 ID
 *    交给 Agent"，会话创建、消息状态、工具轨迹全部由 `AgentConsolePage` 独占。
 *    集中到一个函数后，这条边界是**可检查的**——业务页面里搜不到 `api.agent.*`。
 * 2. **只传 ID 和页面来源，绝不传正文**（2.6）。`AgentEntryContext` 里没有
 *    `raw_text` 这类字段，从类型上就传不出去。
 * 3. 参数名 (`jd_id` / `resume_id` / `application_id`) 与后端
 *    `CreateSessionRequest` 一致，页面不需要各自记一遍。
 */
export const AGENT_ROUTE = "/agent";

/** 查询串里允许出现的键。多余键一律丢弃，避免把无关数据带进 URL（会进浏览器历史）。 */
const ALLOWED_KEYS = [
  "entry_source",
  "jd_id",
  "resume_id",
  "application_id",
  "session_id",
] as const;

export type AgentEntryParams = AgentEntryContext & {
  /** 传了就按它恢复已有会话（刷新 / 从历史返回），不传则新建或复用同上下文会话。 */
  session_id?: string | null;
};

/**
 * 构造 `/agent` 的 URL。**纯函数**——便于直接断言"哪些字段会进 URL"。
 *
 * 空值会被剔除：`jd_id=""` 这种空串如果留着，后端会当成"传了一个空 ID"，
 * 而不是"没传"，进而做出错误的上下文判断。
 */
export function buildCareerAgentUrl(entry: AgentEntryParams): string {
  const qs = new URLSearchParams();
  for (const key of ALLOWED_KEYS) {
    const value = entry[key];
    if (value === undefined || value === null || value === "") continue;
    qs.set(key, String(value));
  }
  const query = qs.toString();
  return query ? `${AGENT_ROUTE}?${query}` : AGENT_ROUTE;
}

/** 从当前 location.search 解析入口参数（页面侧用）。 */
export function parseCareerAgentParams(search: string): AgentEntryParams {
  const qs = new URLSearchParams(search);
  const pick = (key: string): string | null => {
    const v = qs.get(key);
    return v ? v : null;
  };
  return {
    entry_source: pick("entry_source") ?? "unknown",
    jd_id: pick("jd_id"),
    resume_id: pick("resume_id"),
    application_id: pick("application_id"),
    session_id: pick("session_id"),
  };
}

/** React 页面里用的入口：保留 SPA 跳转，不整页刷新。 */
export function useCareerAgentEntry(): (entry: AgentEntryParams) => void {
  const navigate = useNavigate();
  return useCallback(
    (entry: AgentEntryParams) => {
      navigate(buildCareerAgentUrl(entry));
    },
    [navigate],
  );
}

/**
 * 非组件环境（例如事件回调里拿不到 router）使用的入口。
 * 组件内请优先用 `useCareerAgentEntry()`。
 */
export function openCareerAgent(entry: AgentEntryParams): void {
  window.location.assign(buildCareerAgentUrl(entry));
}
