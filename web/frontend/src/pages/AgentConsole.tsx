import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { api, ApiError } from "../api/client";
import { parseCareerAgentParams } from "../agent/openCareerAgent";
import { STATUS_LABELS } from "../constants/application";
import {
  Badge,
  Card,
  EmptyState,
  Icons,
  Loading,
  PageHeader,
  SectionTitle,
} from "../components/ui";
import type {
  AgentActionIntent,
  AgentMemory,
  AgentRun,
  AgentSession,
  AgentTrace,
  Application,
  JDItem,
  ResumeItem,
} from "../types";

/**
 * 求职 Agent —— **三栏页面**（设计文档 20.2 阶段 5；beta 内嵌确认卡与记忆面板）。
 *
 * 重设计（参考成熟对话产品的三栏布局）：
 *   - 左栏：会话列表（localStorage 持久化，刷新/重开浏览器不丢），一键新建会话。
 *   - 中栏：对话流——选上下文 → 问 Agent → 本次运行结果作为一轮对话气泡呈现。
 *   - 右栏：当前上下文 + 运行轨迹 + 长期记忆；thread_id / context_hash 等技术字段
 *     默认折叠进「高级」里，界面不再堆满调试信息（testid 仍保留在 DOM 中）。
 *
 * alpha 明确不做三栏 Console、不做会话列表、不做移动端专门适配（20.5）；beta 也不做。
 * 这一版是**纯前端布局重排**，没有改动任何服务端契约：所有事实仍直接来自服务端响应，
 * 页面自己不做任何推断（22.1 阶段 5 的不通过条件：「页面状态与服务端状态不一致」）。
 *
 * beta 两条具体化（保留）：
 * - 确认卡上显示的「从 X → Y」取自 `pending_action.expected_status / new_status`
 *   （服务端填的），**不用本地业务数据拼一份**；
 * - 判断"为什么没写成"读 `write_failure.code`，**不去匹配 `errors` 里的中文句子**。
 */

const DECISION_LABELS: Record<string, string> = {
  apply_now: "立即投递",
  optimize_first: "先优化简历",
  collect_evidence: "先补证据",
  wait: "暂缓",
  skip: "跳过",
};

const RUN_STATUS_LABELS: Record<string, string> = {
  completed: "已完成",
  failed: "失败",
  needs_input: "需要补充上下文",
  timeout: "超时",
  waiting_confirmation: "等待你确认",
  cancelled: "已取消",
};

/** 待确认意图的状态（服务端 `ActionIntent.status`）。 */
const INTENT_STATUS_LABELS: Record<string, string> = {
  pending_confirmation: "等待你确认",
  confirmed: "已确认",
  succeeded: "已执行",
  cancelled: "已取消",
  expired: "已过期",
  failed: "执行失败",
};

const MEMORY_STATUS_LABELS: Record<string, string> = {
  proposed: "待确认",
  confirmed: "已确认",
  superseded: "已被取代",
  rejected: "已拒绝",
  expired: "已过期",
};

const MEMORY_TYPE_LABELS: Record<string, string> = {
  preference: "偏好",
  decision: "决策",
  outcome: "结果",
  constraint: "约束",
  fact: "事实",
};

/**
 * `write_failure.code` → 一句人话解释。
 *
 * 表里**没有**的取值会原样显示 code，不猜、不兜底成"未知错误"——新情况被看见
 * 比被圆过去重要。每条都写清"本轮没有写入"，因为用户在失败时最需要知道的
 * 就是业务数据到底动没动。
 */
const WRITE_FAILURE_HINTS: Record<string, string> = {
  status_conflict:
    "版本冲突：在你看到这条建议之后，这条投递记录已经被改过（你自己或另一个端改的都可能）。"
    + "为避免覆盖那次改动，本轮没有写入任何数据。请重新问一次 Agent 拿最新的建议。",
  invalid_status: "目标状态不合法，本轮没有写入任何数据。",
  status_not_agent_writable: "这个状态不允许由 Agent 写入，本轮没有写入任何数据。",
  not_found: "找不到这条投递记录（或它不属于你），本轮没有写入任何数据。",
  write_failed: "写库时出错，本轮没有写入任何数据。",
  action_expired: "确认已过期，本轮没有写入任何数据。",
  action_already_resolved: "这条确认已经处理过了，本轮没有重复写入。",
};

/** 会话状态 → Badge 颜色。服务端给了新状态值时走 gray，不隐藏也不猜。 */
function statusColor(status: string): "green" | "red" | "amber" | "gray" {
  if (status === "completed") return "green";
  if (status === "failed") return "red";
  if (status === "needs_input" || status === "waiting_confirmation") return "amber";
  return "gray";
}

function toolStatusColor(status: string): "green" | "red" | "gray" {
  if (status === "succeeded") return "green";
  if (status === "failed") return "red";
  return "gray";
}

const INTENT_STATUS_COLORS: Record<string, "green" | "red" | "amber" | "gray"> = {
  succeeded: "green",
  failed: "red",
  expired: "red",
  cancelled: "gray",
  pending_confirmation: "amber",
  confirmed: "amber",
};

/** Checkpointer 类型 → 一句人话（会话能不能跨重启恢复）。 */
const CHECKPOINTER_HINTS: Record<string, string> = {
  postgres: "会话与确认断点持久化在数据库，服务重启后仍可恢复",
  memory: "会话只存在进程内存里，服务重启后无法恢复",
  uninitialized: "Agent 运行时尚未初始化，会话不可恢复",
};

function statusLabel(status?: string | null): string {
  if (!status) return "—";
  return STATUS_LABELS[status] ?? status;
}

export default function AgentConsole() {
  const [searchParams, setSearchParams] = useSearchParams();
  const queryKey = searchParams.toString();
  const entry = useMemo(() => parseCareerAgentParams(queryKey), [queryKey]);

  const [jdList, setJdList] = useState<JDItem[]>([]);
  const [resumeList, setResumeList] = useState<ResumeItem[]>([]);
  // 投递列表只用来把 action 里的 application_id 显示成人看得懂的公司·职位。
  // 它**不参与**任何判断——确认卡上的"从 X → Y"完全取自服务端的意图参数。
  const [applicationList, setApplicationList] = useState<Application[]>([]);
  const [pickedJd, setPickedJd] = useState(entry.jd_id ?? "");
  const [pickedResume, setPickedResume] = useState(entry.resume_id ?? "");

  const [session, setSession] = useState<AgentSession | null>(null);
  const [run, setRun] = useState<AgentRun | null>(null);
  const [trace, setTrace] = useState<AgentTrace | null>(null);

  // ---- beta：待确认写操作 + 长期记忆（都只存服务端给的值）------------------
  const [pendingActions, setPendingActions] = useState<AgentActionIntent[]>([]);
  const [memories, setMemories] = useState<AgentMemory[]>([]);
  const [memoryInput, setMemoryInput] = useState("");
  const [memoryBusy, setMemoryBusy] = useState(false);
  /** 确认/拒绝过程中的临时状态，避免按钮连点。 */
  const [deciding, setDeciding] = useState(false);
  /** 记忆操作的失败提示：与页面级 error 分开，否则会互相顶掉。 */
  const [memoryError, setMemoryError] = useState<string | null>(null);

  const [message, setMessage] = useState("我下一步该做什么？");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // ---- 重设计：会话列表（localStorage 持久化）+ 高级字段折叠 --------------
  const [showAdvanced, setShowAdvanced] = useState(false);
  const LS_KEY = "of_agent_sessions_v1";
  const [localSessions, setLocalSessions] = useState<
    { session_id: string; context: string; updated_at: number }[]
  >([]);

  // 同一组入口参数只自动创建一次会话。StrictMode 下 effect 会跑两遍，
  // 且两次请求可能同时发出——后端"同上下文复用"要到第一条落库后才生效，
  // 并发下会真的建出两个会话，所以这里必须自己挡一道。
  const bootstrappedRef = useRef<string | null>(null);

  const notify = useCallback((e: unknown) => {
    setError(e instanceof Error ? e.message : String(e));
  }, []);

  // ---- 上下文候选列表（选择器用）------------------------------------------
  useEffect(() => {
    let alive = true;
    void (async () => {
      try {
        const [jd, resume, applications] = await Promise.all([
          api.jd.list(),
          api.resume.list(),
          api.applications.list(),
        ]);
        if (!alive) return;
        setJdList(jd.data ?? []);
        setResumeList(resume.data ?? []);
        setApplicationList(applications.data ?? []);
      } catch (e) {
        if (alive) notify(e);
      }
    })();
    return () => {
      alive = false;
    };
  }, [notify]);

  // ---- 重设计：进入页面时从 localStorage 恢复会话列表 ----------------------
  useEffect(() => {
    try {
      const raw = localStorage.getItem(LS_KEY);
      if (raw) setLocalSessions(JSON.parse(raw));
    } catch {
      /* localStorage 不可用时静默忽略，不影响主流程 */
    }
  }, []);

  const jdName = useCallback(
    (id: string) => {
      const item = jdList.find((j) => j.id === id);
      if (!item) return id;
      return [item.company, item.position].filter(Boolean).join(" · ") || id;
    },
    [jdList],
  );

  const resumeName = useCallback(
    (id: string) => {
      const item = resumeList.find((r) => r.id === id);
      if (!item) return id;
      return `${item.source_file || "简历"} · v${item.version}`;
    },
    [resumeList],
  );

  /** application_id → 公司·职位。**只影响显示**，拿不到就退回 id。 */
  const applicationName = useCallback(
    (id?: string | null) => {
      if (!id) return "—";
      const item = applicationList.find((a) => a.id === id);
      if (!item) return id;
      return [item.company, item.position].filter(Boolean).join(" · ") || id;
    },
    [applicationList],
  );

  const applySession = useCallback((next: AgentSession | null) => {
    setSession(next);
  }, []);

  /** 把会话记进 localStorage 列表（最新置顶，最多 20 条；失败静默）。 */
  const rememberSession = useCallback(
    (s: AgentSession | null) => {
      if (!s) return;
      const context =
        [
          s.context_refs.find((r) => r.type === "jd")
            ? jdName(s.context_refs.find((r) => r.type === "jd")!.id)
            : "",
          s.context_refs.find((r) => r.type === "resume")
            ? resumeName(s.context_refs.find((r) => r.type === "resume")!.id)
            : "",
        ]
          .filter(Boolean)
          .join(" · ") || "未选择上下文";

      setLocalSessions((prev) => {
        const next = prev.filter((x) => x.session_id !== s.session_id);
        const list = [
          { session_id: s.session_id, context, updated_at: Date.now() },
          ...next,
        ].slice(0, 20);
        try {
          localStorage.setItem(LS_KEY, JSON.stringify(list));
        } catch {
          /* ignore */
        }
        return list;
      });
    },
    [jdName, resumeName],
  );

  /** 从左侧会话列表点开一个历史会话。 */
  const restoreSession = useCallback(
    async (sessionId: string) => {
      setBusy(true);
      setError(null);
      try {
        const res = await api.agent.getSession(sessionId);
        if (res.data) {
          applySession(res.data);
          setPickedJd(res.data.context_refs.find((r) => r.type === "jd")?.id ?? "");
          setPickedResume(
            res.data.context_refs.find((r) => r.type === "resume")?.id ?? "",
          );
          const next = new URLSearchParams(queryKey);
          next.set("session_id", res.data.session_id);
          setSearchParams(next, { replace: true });
        }
      } catch (e) {
        notify(e);
      } finally {
        setBusy(false);
      }
    },
    [applySession, notify, queryKey, setSearchParams],
  );

  // ---- 恢复：URL 里带 session_id 就按它恢复（刷新 / 从历史返回）------------
  useEffect(() => {
    if (!entry.session_id) return;
    let alive = true;
    void (async () => {
      setBusy(true);
      setError(null);
      try {
        const res = await api.agent.getSession(entry.session_id as string);
        if (!alive) return;
        applySession(res.data ?? null);
        if (res.data) {
          rememberSession(res.data);
          setPickedJd(
            res.data.context_refs.find((r) => r.type === "jd")?.id ?? "",
          );
          setPickedResume(
            res.data.context_refs.find((r) => r.type === "resume")?.id ?? "",
          );
        }
      } catch (e) {
        if (alive) notify(e);
      } finally {
        if (alive) setBusy(false);
      }
    })();
    return () => {
      alive = false;
    };
  }, [entry.session_id, applySession, notify, rememberSession]);

  // ---- 自动创建：带了上下文就创建（缺什么由后端说了算）--------------------
  const createSession = useCallback(
    async (jdId: string, resumeId: string) => {
      const created = await api.agent.createSession({
        entry_source: entry.entry_source,
        jd_id: jdId || null,
        resume_id: resumeId || null,
        application_id: entry.application_id,
      });
      const data = created.data;
      if (!data) {
        throw new Error("服务端未返回会话");
      }
      applySession(data);
      rememberSession(data);
      setRun(null);
      setTrace(null);
      // 把服务端给的 session_id 写回 URL：刷新后能恢复，且页面与地址栏一致
      const next = new URLSearchParams(queryKey);
      next.set("session_id", data.session_id);
      setSearchParams(next, { replace: true });
      return data;
    },
    [applySession, entry.application_id, entry.entry_source, queryKey, rememberSession, setSearchParams],
  );

  useEffect(() => {
    if (entry.session_id) return;
    if (!entry.jd_id && !entry.resume_id) return;
    const signature = `${entry.entry_source}|${entry.jd_id ?? ""}|${entry.resume_id ?? ""}`;
    if (bootstrappedRef.current === signature) return;
    bootstrappedRef.current = signature;
    let alive = true;
    void (async () => {
      setBusy(true);
      setError(null);
      try {
        await createSession(entry.jd_id ?? "", entry.resume_id ?? "");
      } catch (e) {
        if (alive) notify(e);
      } finally {
        if (alive) setBusy(false);
      }
    })();
    return () => {
      alive = false;
    };
  }, [entry.session_id, entry.jd_id, entry.resume_id, entry.entry_source, createSession, notify]);

  // ---- beta：拉取待确认写操作与长期记忆 -----------------------------------
  // 定义在 runMessage 之前：runMessage 跑完要重拉这两块，先声明才用得到。
  const refreshPendingActions = useCallback(async (sessionId: string) => {
    const res = await api.agent.listActions(sessionId);
    setPendingActions(res.data ?? []);
  }, []);

  const refreshMemories = useCallback(async () => {
    const res = await api.agent.listMemories();
    setMemories(res.data ?? []);
  }, []);

  // ---- 运行一条消息 -------------------------------------------------------
  const runMessage = useCallback(async () => {
    if (!session) return;
    const text = message.trim();
    if (!text) return;
    setBusy(true);
    setError(null);
    try {
      const res = await api.agent.runMessage(session.session_id, text);
      setRun(res.data ?? null);
      // 运行完**重新拉一次会话**：会话级事实（status / run_count / last_output）
      // 必须以服务端为准，不能用本地推断顶替。
      // 同时重拉待确认列表与记忆：本轮可能刚提出一个写操作、也可能刚落一条决策记忆。
      const [fresh] = await Promise.all([
        api.agent.getSession(session.session_id),
        refreshPendingActions(session.session_id),
        refreshMemories(),
      ]);
      setSession(fresh.data ?? null);
    } catch (e) {
      notify(e);
    } finally {
      setBusy(false);
    }
  }, [message, notify, refreshMemories, refreshPendingActions, session]);

  const loadTrace = useCallback(async () => {
    if (!session) return;
    setBusy(true);
    setError(null);
    try {
      const res = await api.agent.trace(session.session_id);
      setTrace(res.data ?? null);
    } catch (e) {
      notify(e);
    } finally {
      setBusy(false);
    }
  }, [notify, session]);

  // ---- beta：待确认列表与记忆的首次加载 -----------------------------------
  // 刷新页面恢复会话时也要拉一次：checkpointer 让确认断点跨重启存活，
  // 页面必须能把那个断点重新显示出来，否则会出现「服务端在等你确认，
  // 页面上却什么都没有」的悬空状态。
  useEffect(() => {
    if (!session) {
      setPendingActions([]);
      return;
    }
    let alive = true;
    void (async () => {
      try {
        const res = await api.agent.listActions(session.session_id);
        if (alive) setPendingActions(res.data ?? []);
      } catch (e) {
        // 拉不到待确认列表只影响这一块，不能把整页状态打成错误
        if (alive) setMemoryError(e instanceof Error ? e.message : String(e));
      }
    })();
    return () => {
      alive = false;
    };
  }, [session]);

  useEffect(() => {
    let alive = true;
    void (async () => {
      try {
        const res = await api.agent.listMemories();
        if (alive) setMemories(res.data ?? []);
      } catch (e) {
        if (alive) setMemoryError(e instanceof Error ? e.message : String(e));
      }
    })();
    return () => {
      alive = false;
    };
  }, []);

  // ---- beta：确认 / 拒绝一个待执行写操作 -----------------------------------
  const decide = useCallback(
    async (actionId: string, approved: boolean) => {
      if (!session) return;
      setDeciding(true);
      setError(null);
      try {
        const res = await api.agent.decideAction(session.session_id, actionId, approved);
        setRun(res.data ?? null);
        // 和服务端对齐：会话状态、待确认列表、记忆（写成功后落结果记忆）都重拉
        const [fresh] = await Promise.all([
          api.agent.getSession(session.session_id),
          refreshPendingActions(session.session_id),
          refreshMemories(),
        ]);
        setSession(fresh.data ?? null);
      } catch (e) {
        // 410 = 已过期。**按状态码分支**，不去匹配服务端的中文文案。
        if (e instanceof ApiError && e.status === 410) {
          setError("这条确认已过期，本轮没有写入任何数据。请重新问一次 Agent。");
          void refreshPendingActions(session.session_id).catch(() => undefined);
        } else {
          notify(e);
        }
      } finally {
        setDeciding(false);
      }
    },
    [notify, refreshMemories, refreshPendingActions, session],
  );

  // ---- beta：记忆的增 / 确认 / 删 -----------------------------------------
  const addMemory = useCallback(async () => {
    const content = memoryInput.trim();
    if (!content) return;
    setMemoryBusy(true);
    setMemoryError(null);
    try {
      await api.agent.createMemory({ content });
      setMemoryInput("");
      await refreshMemories();
    } catch (e) {
      setMemoryError(e instanceof Error ? e.message : String(e));
    } finally {
      setMemoryBusy(false);
    }
  }, [memoryInput, refreshMemories]);

  const confirmMemory = useCallback(
    async (memoryId: string) => {
      setMemoryBusy(true);
      setMemoryError(null);
      try {
        await api.agent.confirmMemory(memoryId);
        await refreshMemories();
      } catch (e) {
        setMemoryError(e instanceof Error ? e.message : String(e));
      } finally {
        setMemoryBusy(false);
      }
    },
    [refreshMemories],
  );

  const deleteMemory = useCallback(
    async (memoryId: string) => {
      setMemoryBusy(true);
      setMemoryError(null);
      try {
        await api.agent.deleteMemory(memoryId);
        await refreshMemories();
      } catch (e) {
        setMemoryError(e instanceof Error ? e.message : String(e));
      } finally {
        setMemoryBusy(false);
      }
    },
    [refreshMemories],
  );

  const missing = session?.missing_context ?? [];

  // beta：当前要展示的待确认意图。两个来源**都是服务端给的**——
  // 优先用刚跑完那一轮返回的（信息最新），刷新页面后则用待确认列表里的。
  // 两者都不做本地构造。
  const activeIntent: AgentActionIntent | null =
    run?.pending_action ?? pendingActions[0] ?? null;

  // 记忆面板：待确认与已确认**必须分开**。混在一起会让人以为
  // "模型推断的东西"已经是记录在案的事实。
  const proposedMemories = memories.filter((m) => m.status === "proposed");
  const confirmedMemories = memories.filter((m) => m.status === "confirmed");

  const pickedContextChanged =
    !!session &&
    ((pickedJd || "") !== (session.context_refs.find((r) => r.type === "jd")?.id ?? "") ||
      (pickedResume || "") !== (session.context_refs.find((r) => r.type === "resume")?.id ?? ""));

  return (
    <div className="mx-auto max-w-7xl px-4 py-4">
      <PageHeader
        title="求职 Agent"
        subtitle="读 JD 与简历给出行动建议；改业务数据的操作必须经你确认"
        icon={Icons.sparkle}
        actions={
          <div className="flex items-center gap-2">
            {/* 两个徽章都读服务端字段：不写死"只读"或"已启用"，否则后端一改就骗人 */}
            <Badge color={session?.checkpointer === "postgres" ? "green" : "gray"}>
              <span data-testid="agent-checkpointer">
                {session?.checkpointer === "postgres"
                  ? "会话可恢复"
                  : session?.checkpointer === "memory"
                    ? "会话不持久"
                    : session
                      ? `运行时 ${session.checkpointer}`
                      : "尚未建会话"}
              </span>
            </Badge>
            <Badge color={session?.memory_enabled ? "indigo" : "gray"}>
              <span data-testid="agent-memory-enabled">
                {session?.memory_enabled ? "长期记忆已启用" : "长期记忆未启用"}
              </span>
            </Badge>
          </div>
        }
      />

      {error && (
        <div className="of-alert-error mb-4" data-testid="agent-error">
          {error}
        </div>
      )}

      <div className="grid grid-cols-1 gap-4 lg:grid-cols-[260px_minmax(0,1fr)_360px]">
        {/* ================= 左栏：会话列表 ================= */}
        <aside className="self-start lg:sticky lg:top-4 lg:max-h-[calc(100vh-2rem)] lg:overflow-y-auto">
          <Card className="p-4">
            <div className="mb-3 flex items-center justify-between">
              <SectionTitle>会话</SectionTitle>
              <button
                className="of-btn-ghost text-xs"
                disabled={busy}
                onClick={() => {
                  bootstrappedRef.current = null;
                  const next = new URLSearchParams(queryKey);
                  next.delete("session_id");
                  setSearchParams(next, { replace: true });
                  applySession(null);
                  setRun(null);
                  setTrace(null);
                }}
                data-testid="agent-new-session"
              >
                新建
              </button>
            </div>
            {localSessions.length === 0 ? (
              <p className="text-xs text-slate-400">还没有保存的会话。</p>
            ) : (
              <ul className="space-y-1">
                {localSessions.map((s) => (
                  <li key={s.session_id}>
                    <button
                      className="w-full rounded-lg border border-slate-200 px-2 py-2 text-left text-xs hover:bg-slate-50"
                      onClick={() => void restoreSession(s.session_id)}
                    >
                      <span className="block truncate font-medium text-slate-700">
                        {s.context}
                      </span>
                      <span className="text-[11px] text-slate-400">
                        {new Date(s.updated_at).toLocaleString()}
                      </span>
                    </button>
                  </li>
                ))}
              </ul>
            )}
          </Card>
        </aside>

        {/* ================= 中栏：对话流 ================= */}
        <section className="min-w-0 space-y-4">
          {/* 上下文选择器 */}
          <Card className="p-5">
            <SectionTitle>选择上下文</SectionTitle>

            {missing.length > 0 && (
              <div className="of-alert-error mb-4" data-testid="agent-needs-input">
                上下文不完整，Agent 不会猜测：
                {missing.map((m) => (
                  <span key={m.type} className="ml-2" data-testid="agent-missing-item">
                    {m.type}
                    {m.hint ? `（${m.hint}）` : ""}
                  </span>
                ))}
              </div>
            )}

            <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
              <label className="block">
                <span className="mb-1 block text-xs text-slate-500">目标 JD</span>
                <select
                  className="of-select w-full"
                  value={pickedJd}
                  onChange={(e) => setPickedJd(e.target.value)}
                  data-testid="agent-jd-select"
                >
                  <option value="">请选择 JD</option>
                  {jdList.map((j) => (
                    <option key={j.id} value={j.id}>
                      {[j.company, j.position].filter(Boolean).join(" · ") || j.id}
                    </option>
                  ))}
                </select>
              </label>

              <label className="block">
                <span className="mb-1 block text-xs text-slate-500">使用简历</span>
                <select
                  className="of-select w-full"
                  value={pickedResume}
                  onChange={(e) => setPickedResume(e.target.value)}
                  data-testid="agent-resume-select"
                >
                  <option value="">请选择简历</option>
                  {resumeList.map((r) => (
                    <option key={r.id} value={r.id}>
                      {resumeName(r.id)}
                    </option>
                  ))}
                </select>
              </label>
            </div>

            {pickedContextChanged && (
              <p className="mt-3 text-xs text-amber-600" data-testid="agent-context-switch-hint">
                所选上下文与当前会话不同。切换上下文会新建一个会话，当前会话保持不变，
                不会静默替换上下文。
              </p>
            )}

            <div className="mt-4 flex flex-wrap gap-2">
              <button
                className="of-btn-primary"
                disabled={busy}
                onClick={() => {
                  bootstrappedRef.current = `${entry.entry_source}|${pickedJd}|${pickedResume}`;
                  setBusy(true);
                  setError(null);
                  void createSession(pickedJd, pickedResume)
                    .catch(notify)
                    .finally(() => setBusy(false));
                }}
                data-testid="agent-create-session"
              >
                {session ? "以所选上下文新建会话" : "开始会话"}
              </button>
              {session && (
                <button
                  className="of-btn-outline"
                  disabled={busy}
                  onClick={() => {
                    const next = new URLSearchParams(queryKey);
                    next.delete("session_id");
                    setSearchParams(next, { replace: true });
                    applySession(null);
                    setRun(null);
                    setTrace(null);
                    bootstrappedRef.current = null;
                  }}
                  data-testid="agent-reset"
                >
                  清空本页会话
                </button>
              )}
            </div>
          </Card>

          {/* 输入 */}
          <Card className="p-5">
            <SectionTitle>问 Agent</SectionTitle>
            <textarea
              className="of-textarea w-full"
              rows={3}
              value={message}
              onChange={(e) => setMessage(e.target.value)}
              placeholder="例如：这个岗位值得投吗？下一步该做什么？"
              data-testid="agent-message"
            />
            <div className="mt-3 flex items-center gap-3">
              <button
                className="of-btn-primary"
                disabled={busy || !session || message.trim().length === 0}
                onClick={() => void runMessage()}
                data-testid="agent-send"
              >
                {busy ? "运行中…" : "运行"}
              </button>
              <span className="text-xs text-slate-400">
                同一会话同一时刻只能有一条消息在执行（按钮已禁用，避免重复提交）。
              </span>
            </div>
          </Card>

          {busy && <Loading text="Agent 运行中…" />}

          {/* ---------------- 运行结果（对话气泡） ---------------- */}
          {run && (
            <Card className="p-5">
              <SectionTitle
                right={
                  <div className="flex items-center gap-2">
                    <Badge color={statusColor(run.status)}>
                      <span data-testid="agent-run-status">
                        {RUN_STATUS_LABELS[run.status] ?? run.status}
                      </span>
                    </Badge>
                    <Badge color="gray">
                      <span data-testid="agent-run-id">{run.run_id}</span>
                    </Badge>
                  </div>
                }
              >
                本次运行结果
              </SectionTitle>
              <span className="sr-only" data-testid="agent-result" />

              {run.output.decision && (
                <div className="mb-3 flex flex-wrap items-center gap-3">
                  <span className="text-sm text-slate-500">决策</span>
                  <Badge color="indigo">
                    <span data-testid="agent-decision">
                      {DECISION_LABELS[run.output.decision] ?? run.output.decision}
                    </span>
                  </Badge>
                  <span className="text-xs text-slate-500">
                    置信度{" "}
                    <span data-testid="agent-confidence">{run.output.confidence}</span>
                    （服务端原值，未做百分比换算）
                  </span>
                  <span className="text-xs text-slate-500">
                    耗时 <span data-testid="agent-latency">{run.latency_ms}</span> ms
                  </span>
                </div>
              )}

              <p className="text-sm leading-relaxed text-slate-700" data-testid="agent-summary">
                {run.output.summary || "（本次运行没有给出结论文字）"}
              </p>

              {run.output.missing_info.length > 0 && (
                <div className="mt-3 rounded-lg bg-amber-50 p-3 text-xs text-amber-700">
                  <p className="font-medium">需要你补充</p>
                  <ul className="mt-1 list-disc pl-4" data-testid="agent-missing-info">
                    {run.output.missing_info.map((m) => (
                      <li key={m}>{m}</li>
                    ))}
                  </ul>
                </div>
              )}

              {run.errors.length > 0 && (
                <div className="of-alert-error mt-3" data-testid="agent-run-errors">
                  {run.errors.map((e) => (
                    <p key={e}>{e}</p>
                  ))}
                </div>
              )}

              {/* 行动建议：alpha 只给建议，不允许执行（20.4） */}
              {run.output.actions.length > 0 && (
                <div className="mt-4">
                  <p className="mb-2 text-xs font-medium text-slate-500">
                    行动建议（本节只是建议文案；需要改业务数据的操作会单独出现在下方
                    「待确认的写操作」里，**点了确认才会执行**）
                  </p>
                  <div className="space-y-2">
                    {run.output.actions.map((a) => (
                      <div
                        key={`${a.type}-${a.title}`}
                        className="rounded-xl border border-slate-200 p-3"
                        data-testid="agent-action"
                      >
                        <div className="flex items-center gap-2">
                          <Badge color="blue">P{a.priority}</Badge>
                          <span className="text-sm font-medium text-slate-800">{a.title}</span>
                        </div>
                        {a.reason && <p className="mt-1 text-xs text-slate-500">{a.reason}</p>}
                        {a.evidence_refs.length > 0 && (
                          <p className="mt-1 text-xs text-slate-400">
                            依据：
                            {a.evidence_refs.map((ref) => (
                              <span key={ref} className="mr-1 font-mono" data-testid="agent-action-evidence">
                                {ref}
                              </span>
                            ))}
                          </p>
                        )}
                      </div>
                    ))}
                  </div>
                </div>
              )}

              {/* 工具调用轨迹 */}
              {run.tool_calls.length > 0 && (
                <div className="mt-4">
                  <p className="mb-2 text-xs font-medium text-slate-500">工具调用</p>
                  <div className="space-y-1" data-testid="agent-tool-calls">
                    {run.tool_calls.map((t, i) => (
                      <div
                        key={`${t.tool}-${i}`}
                        className="flex items-center gap-2 text-xs text-slate-600"
                        data-testid="agent-tool-call"
                      >
                        <Badge color={toolStatusColor(t.status)}>{t.status}</Badge>
                        <span className="font-mono">{t.tool}</span>
                        <span className="text-slate-400">{t.latency_ms} ms</span>
                        {t.error_code && <span className="text-rose-500">{t.error_code}</span>}
                      </div>
                    ))}
                  </div>
                </div>
              )}
            </Card>
          )}

          {/* ================= 待确认的写操作（beta） =========================
              只在**服务端说有待确认意图**时出现，不出现空壳卡片。
              两个来源都来自服务端：刚跑完那轮的 `pending_action`，或刷新页面后
              从 `GET /sessions/{id}/actions` 取回的待确认列表。 */}
          {activeIntent && (
            <Card className="border-amber-300 p-5">
              <span className="sr-only" data-testid="agent-pending-action" />
              <SectionTitle
                right={
                  <Badge color={INTENT_STATUS_COLORS[activeIntent.status] ?? "gray"}>
                    <span data-testid="agent-intent-status">
                      {INTENT_STATUS_LABELS[activeIntent.status] ?? activeIntent.status}
                    </span>
                  </Badge>
                }
              >
                待确认的写操作
              </SectionTitle>

              <p className="text-sm text-slate-700" data-testid="agent-intent-summary">
                把「
                <span className="font-medium">{applicationName(activeIntent.application_id)}</span>
                」的投递状态从{" "}
                <span className="font-medium" data-testid="agent-intent-expected">
                  {statusLabel(activeIntent.expected_status)}
                </span>{" "}
                改为{" "}
                <span className="font-medium" data-testid="agent-intent-new">
                  {statusLabel(activeIntent.new_status)}
                </span>
                。
              </p>
              <p className="mt-1 text-xs text-slate-400">
                以上「从 / 改为」是**服务端给的**，与真正会写入的内容一致。
              </p>

              {activeIntent.reason && (
                <p className="mt-2 text-sm text-slate-600" data-testid="agent-intent-reason">
                  理由：{activeIntent.reason}
                </p>
              )}

              <dl className="mt-3 grid grid-cols-1 gap-x-6 gap-y-1 text-xs text-slate-500 sm:grid-cols-2">
                <div>
                  <dt className="text-slate-400">操作 ID</dt>
                  <dd className="break-all font-mono" data-testid="agent-intent-id">
                    {activeIntent.action_id}
                  </dd>
                </div>
                <div>
                  <dt className="text-slate-400">工具</dt>
                  <dd className="font-mono" data-testid="agent-intent-tool">
                    {activeIntent.tool}
                  </dd>
                </div>
                <div>
                  <dt className="text-slate-400">过期时间</dt>
                  <dd data-testid="agent-intent-expires">
                    {activeIntent.expires_at ?? "—"}
                  </dd>
                </div>
              </dl>

              {activeIntent.status === "pending_confirmation" ? (
                <div className="mt-4 flex flex-wrap items-center gap-3">
                  <button
                    className="of-btn-primary"
                    disabled={deciding}
                    onClick={() => void decide(activeIntent.action_id, true)}
                    data-testid="agent-intent-approve"
                  >
                    {deciding ? "处理中…" : "确认执行"}
                  </button>
                  <button
                    className="of-btn-outline"
                    disabled={deciding}
                    onClick={() => void decide(activeIntent.action_id, false)}
                    data-testid="agent-intent-reject"
                  >
                    取消，不要改
                  </button>
                  <span className="text-xs text-slate-400">
                    确认前不会改动任何业务数据；不确认也不会自动执行。
                  </span>
                </div>
              ) : (
                <p className="mt-4 text-xs text-slate-500" data-testid="agent-intent-resolved">
                  这项操作已经处理过了（当前状态：
                  {INTENT_STATUS_LABELS[activeIntent.status] ?? activeIntent.status}）。
                  如果列表没刷新，请点下方「刷新待确认列表」。
                </p>
              )}

              <div className="mt-3">
                <button
                  className="of-btn-ghost"
                  disabled={deciding}
                  onClick={() => {
                    if (session) void refreshPendingActions(session.session_id).catch(notify);
                  }}
                  data-testid="agent-intent-refresh"
                >
                  刷新待确认列表
                </button>
              </div>
            </Card>
          )}

          {/* 写操作结果：成功 / 失败（失败必须说清"业务数据没动"） */}
          {run && run.wrote_business_data && (
            <Card className="border-emerald-300 p-5">
              <span className="sr-only" data-testid="agent-wrote-business-data" />
              <p className="text-sm text-emerald-700">
                已按你的确认写入业务数据（这一轮 Agent 真的改了投递状态）。
              </p>
            </Card>
          )}

          {run?.write_failure && (
            <Card className="border-rose-300 p-5">
              <span className="sr-only" data-testid="agent-write-failure" />
              <SectionTitle
                right={
                  <Badge color="red">
                    <span data-testid="agent-write-failure-code">
                      {run.write_failure.code}
                    </span>
                  </Badge>
                }
              >
                写操作没有完成
              </SectionTitle>
              <p className="text-sm text-slate-700" data-testid="agent-write-failure-message">
                {run.write_failure.message}
              </p>
              <p className="mt-2 text-xs text-slate-600" data-testid="agent-write-failure-hint">
                {WRITE_FAILURE_HINTS[run.write_failure.code] ??
                  `服务端给出的失败码是 ${run.write_failure.code}，页面没有对应的解释文案（不猜）。`}
              </p>
            </Card>
          )}

          {/* ---------------- 证据 ---------------- */}
          {run && (
            <Card className="p-5">
              <SectionTitle count={run.output.evidence.length}>证据（可追溯）</SectionTitle>
              <span className="sr-only" data-testid="agent-evidence-card" />
              {run.output.evidence.length === 0 ? (
                <EmptyState title="本次运行没有引用证据" description="没有证据支持的结论不会被展示为确定结论。" />
              ) : (
                <div className="space-y-2">
                  {run.output.evidence.map((e) => (
                    <div
                      key={e.ref}
                      className="rounded-xl border border-slate-200 p-3"
                      data-testid="agent-evidence"
                    >
                      <div className="flex flex-wrap items-center gap-2">
                        <span className="font-mono text-xs text-slate-700" data-testid="agent-evidence-ref">
                          {e.ref}
                        </span>
                        <Badge color="cyan">{e.source}</Badge>
                        {e.object_version && (
                          <span className="text-xs text-slate-400" data-testid="agent-evidence-version">
                            {e.object_version}
                          </span>
                        )}
                      </div>
                      {e.summary && (
                        <p className="mt-1 text-xs text-slate-500" data-testid="agent-evidence-summary">
                          {e.summary}
                        </p>
                      )}
                    </div>
                  ))}
                </div>
              )}
            </Card>
          )}
        </section>

        {/* ================= 右栏：上下文 + 轨迹 + 记忆 ================= */}
        <aside className="min-w-0 space-y-4 self-start lg:sticky lg:top-4 lg:max-h-[calc(100vh-2rem)] lg:overflow-y-auto">
          {/* ---------------- 当前上下文（技术字段折叠） ---------------- */}
          <Card className="p-5">
            <SectionTitle
              right={
                session ? (
                  <Badge color={statusColor(session.status)}>
                    <span data-testid="agent-status">
                      {RUN_STATUS_LABELS[session.status] ?? session.status}
                    </span>
                  </Badge>
                ) : undefined
              }
            >
              当前上下文
            </SectionTitle>

            {!session ? (
              <p className="text-sm text-slate-500" data-testid="agent-no-session">
                还没有会话。选择 JD 与简历后开始，或从业务页面带着上下文进入。
              </p>
            ) : (
              <dl className="grid grid-cols-1 gap-x-6 gap-y-2 text-sm">
                <div>
                  <dt className="text-xs text-slate-400">会话 session_id</dt>
                  <dd className="break-all font-mono text-xs text-slate-700" data-testid="agent-session-id">
                    {session.session_id}
                  </dd>
                </div>
                <div>
                  <dt className="text-xs text-slate-400">目标 JD</dt>
                  <dd className="text-slate-700" data-testid="agent-context-jd">
                    {session.context_refs.find((r) => r.type === "jd")
                      ? jdName(session.context_refs.find((r) => r.type === "jd")!.id)
                      : "未选择"}
                  </dd>
                </div>
                <div>
                  <dt className="text-xs text-slate-400">使用简历</dt>
                  <dd className="text-slate-700" data-testid="agent-context-resume">
                    {session.context_refs.find((r) => r.type === "resume")
                      ? resumeName(session.context_refs.find((r) => r.type === "resume")!.id)
                      : "未选择"}
                  </dd>
                </div>
                <div>
                  <dt className="text-xs text-slate-400">已完成运行数</dt>
                  <dd className="text-slate-700" data-testid="agent-run-count">
                    {session.run_count}
                  </dd>
                </div>
              </dl>
            )}

            {session && (
              <div className="mt-3 space-y-1 text-xs text-slate-500">
                <p data-testid="agent-session-hint">
                  写操作需要你确认后才会执行；拒绝或过期都不会改动业务数据。
                </p>
                <p data-testid="agent-checkpointer-hint">
                  {CHECKPOINTER_HINTS[session.checkpointer] ?? `checkpointer=${session.checkpointer}`}
                </p>
                {session.has_pending_action && (
                  <p className="text-amber-600" data-testid="agent-has-pending-hint">
                    这个会话有一项待你确认的写操作，见下方「待确认的写操作」。
                  </p>
                )}
                {session.warnings.map((w) => (
                  <p key={w} data-testid="agent-warning">
                    {w}
                  </p>
                ))}
              </div>
            )}

            {/* 技术字段折叠：默认隐藏，避免界面堆满调试信息；testid 仍保留在 DOM 中 */}
            <div className="mt-3 border-t border-slate-100 pt-3">
              <button
                className="of-btn-ghost text-xs"
                onClick={() => setShowAdvanced((v) => !v)}
              >
                {showAdvanced ? "收起技术字段" : "展开技术字段"}
              </button>
              <div className={showAdvanced ? "" : "hidden"}>
                {session && (
                  <dl className="mt-2 grid grid-cols-1 gap-x-6 gap-y-2 text-sm">
                    <div>
                      <dt className="text-xs text-slate-400">线程 thread_id（应等于 session_id）</dt>
                      <dd className="break-all font-mono text-xs text-slate-700" data-testid="agent-thread-id">
                        {session.thread_id}
                      </dd>
                    </div>
                    <div>
                      <dt className="text-xs text-slate-400">上下文指纹 context_hash</dt>
                      <dd className="break-all font-mono text-xs text-slate-700" data-testid="agent-context-hash">
                        {session.context_hash ?? "—"}
                      </dd>
                    </div>
                  </dl>
                )}
              </div>
            </div>
          </Card>

          {/* ---------------- 运行轨迹（trace） ---------------- */}
          {session && (
            <Card className="p-5">
              <SectionTitle
                right={
                  <button className="of-btn-ghost" disabled={busy} onClick={() => void loadTrace()} data-testid="agent-load-trace">
                    刷新运行轨迹
                  </button>
                }
              >
                运行轨迹
              </SectionTitle>
              {!trace ? (
                <p className="text-sm text-slate-500">点「刷新运行轨迹」查看该会话历次运行的 run_id 与工具调用。</p>
              ) : trace.runs.length === 0 ? (
                <EmptyState title="还没有运行记录" />
              ) : (
                <div className="space-y-2" data-testid="agent-trace">
                  {trace.runs.map((t) => (
                    <div
                      key={t.run_id}
                      className="flex flex-wrap items-center gap-2 rounded-lg bg-slate-50 px-3 py-2 text-xs text-slate-600"
                      data-testid="agent-trace-run"
                    >
                      <Badge color={statusColor(t.status)}>{RUN_STATUS_LABELS[t.status] ?? t.status}</Badge>
                      <span className="font-mono" data-testid="agent-trace-run-id">
                        {t.run_id}
                      </span>
                      <span className="text-slate-400">{t.tool_calls.length} 次工具调用</span>
                      <span className="text-slate-400">{t.latency_ms} ms</span>
                      {t.decision && <span className="text-slate-400">{DECISION_LABELS[t.decision] ?? t.decision}</span>}
                    </div>
                  ))}
                </div>
              )}
            </Card>
          )}

          {/* ================= 长期记忆（beta） ==============================
              由于「待确认」与「已确认」**分区显示**：只有 confirmed 才进向量索引、
              才会被 Agent 当事实召回。混在一起会让人以为模型猜的东西已经生效。 */}
          <Card className="p-5">
            <span className="sr-only" data-testid="agent-memory-panel" />
            <SectionTitle
              right={
                <button
                  className="of-btn-ghost"
                  disabled={memoryBusy}
                  onClick={() => void refreshMemories().catch(notify)}
                  data-testid="agent-memory-refresh"
                >
                  刷新记忆
                </button>
              }
            >
              长期记忆
            </SectionTitle>

            <p className="text-xs text-slate-500">
              「待确认」是 Agent 从你的简历与 JD 推断出的，**不会被当成事实召回**；
              你确认之后它才进入记忆。你可以随时删除任何一条。
            </p>

            {memoryError && (
              <div className="of-alert-error mt-3" data-testid="agent-memory-error">
                {memoryError}
              </div>
            )}

            <div className="mt-4">
              <p className="mb-2 text-xs font-medium text-slate-500">
                待你确认（<span data-testid="agent-proposed-count">{proposedMemories.length}</span> 条）
              </p>
              {proposedMemories.length === 0 ? (
                <p className="text-sm text-slate-400" data-testid="agent-proposed-empty">
                  没有待确认的记忆。
                </p>
              ) : (
                <div className="space-y-2">
                  {proposedMemories.map((m) => (
                    <div
                      key={m.memory_id}
                      className="rounded-xl border border-amber-200 bg-amber-50/40 p-3"
                      data-testid="agent-memory-proposed"
                    >
                      <div className="flex flex-wrap items-center gap-2">
                        <Badge color="amber">
                          {MEMORY_STATUS_LABELS[m.status] ?? m.status}
                        </Badge>
                        <Badge color="gray">
                          {MEMORY_TYPE_LABELS[m.memory_type] ?? m.memory_type}
                        </Badge>
                        <span className="font-mono text-xs text-slate-400" data-testid="agent-memory-id">
                          {m.memory_id}
                        </span>
                      </div>
                      <p className="mt-1 text-sm text-slate-700" data-testid="agent-memory-content">
                        {m.content}
                      </p>
                      <div className="mt-2 flex flex-wrap gap-2">
                        <button
                          className="of-btn-primary"
                          disabled={memoryBusy}
                          onClick={() => void confirmMemory(m.memory_id)}
                          data-testid="agent-memory-confirm"
                          data-memory-id={m.memory_id}
                        >
                          记入记忆
                        </button>
                        <button
                          className="of-btn-outline"
                          disabled={memoryBusy}
                          onClick={() => void deleteMemory(m.memory_id)}
                          data-testid="agent-memory-delete-proposed"
                          data-memory-id={m.memory_id}
                        >
                          删除
                        </button>
                      </div>
                    </div>
                  ))}
                </div>
              )}
            </div>

            <div className="mt-5">
              <p className="mb-2 text-xs font-medium text-slate-500">
                已生效的记忆（<span data-testid="agent-confirmed-count">{confirmedMemories.length}</span> 条）
              </p>
              {confirmedMemories.length === 0 ? (
                <p className="text-sm text-slate-400" data-testid="agent-confirmed-empty">
                  还没有已确认的记忆。
                </p>
              ) : (
                <div className="space-y-2">
                  {confirmedMemories.map((m) => (
                    <div
                      key={m.memory_id}
                      className="rounded-xl border border-slate-200 p-3"
                      data-testid="agent-memory-confirmed"
                    >
                      <div className="flex flex-wrap items-center gap-2">
                        <Badge color="green">
                          {MEMORY_STATUS_LABELS[m.status] ?? m.status}
                        </Badge>
                        <Badge color="gray">
                          {MEMORY_TYPE_LABELS[m.memory_type] ?? m.memory_type}
                        </Badge>
                        <span className="text-xs text-slate-400" data-testid="agent-memory-source">
                          来源 {m.source}
                        </span>
                        <span className="font-mono text-xs text-slate-400" data-testid="agent-memory-id">
                          {m.memory_id}
                        </span>
                      </div>
                      <p className="mt-1 text-sm text-slate-700" data-testid="agent-memory-content">
                        {m.content}
                      </p>
                      <div className="mt-2">
                        <button
                          className="of-btn-outline"
                          disabled={memoryBusy}
                          onClick={() => void deleteMemory(m.memory_id)}
                          data-testid="agent-memory-delete-confirmed"
                          data-memory-id={m.memory_id}
                        >
                          删除
                        </button>
                      </div>
                    </div>
                  ))}
                </div>
              )}
            </div>

            <div className="mt-5 border-t border-slate-100 pt-4">
              <p className="mb-2 text-xs font-medium text-slate-500">
                直接加一条（你说的话直接生效，不需要再确认）
              </p>
              <textarea
                className="of-textarea w-full"
                rows={2}
                value={memoryInput}
                onChange={(e) => setMemoryInput(e.target.value)}
                placeholder="例如：我只考虑北京的机会"
                data-testid="agent-memory-input"
              />
              <div className="mt-2 flex items-center gap-3">
                <button
                  className="of-btn-primary"
                  disabled={memoryBusy || memoryInput.trim().length === 0}
                  onClick={() => void addMemory()}
                  data-testid="agent-memory-add"
                >
                  添加
                </button>
                <span className="text-xs text-slate-400">
                  内容限 500 字、最多 3 行；长文与凭据类内容会被服务端拒绝。
                </span>
              </div>
            </div>
          </Card>
        </aside>
      </div>
    </div>
  );
}
