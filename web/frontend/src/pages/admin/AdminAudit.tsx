import { useCallback, useEffect, useState } from "react";
import { api } from "../../api/client";
import type { AdminAuditPage } from "../../types";
import { Card, EmptyState, Icons, Loading, PageHeader, SectionTitle, TableScroll } from "../../components/ui";

/** 动作码 -> 中文标签 */
const ACTION_LABELS: Record<string, string> = {
  update_role: "修改角色",
  toggle_active: "启用/禁用账号",
  reset_password: "重置密码",
  bind_email: "绑定邮箱",
  update_setting: "修改系统设置",
  delete_post: "删除帖子",
  delete_comment: "删除评论",
  delete_feedback: "删除反馈",
  delete_corpus: "删除语料",
};

function fmtDetail(detail: string | null): string {
  if (!detail) return "-";
  try {
    const obj = JSON.parse(detail);
    if (obj && typeof obj === "object") {
      if ("字段" in obj) {
        return `${obj["字段"]}：${String(obj["旧值"])} → ${String(obj["新值"])}`;
      }
      if ("说明" in obj) return String(obj["说明"]);
      return JSON.stringify(obj, null, 0);
    }
  } catch {
    /* 非 JSON 直接原样展示 */
  }
  return detail;
}

export default function AdminAudit() {
  const [data, setData] = useState<AdminAuditPage | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [action, setAction] = useState("");
  const [page, setPage] = useState(1);

  const load = useCallback(async (p: number, act: string) => {
    setLoading(true);
    setError("");
    try {
      const res = await api.admin.auditLogs({ page: p, page_size: 30, action: act || undefined });
      if (res.success && res.data) setData(res.data);
      else setError(res.error || "加载失败");
    } catch (err) {
      setError(err instanceof Error ? err.message : "加载失败");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    const t = window.setTimeout(() => void load(page, action), 0);
    return () => window.clearTimeout(t);
  }, [load, page, action]);

  return (
    <div>
      <PageHeader title="审计日志" subtitle="管理员操作留痕" icon={Icons.resume} />

      {error && <div className="of-alert-error mb-4">{error}</div>}

      <div className="mb-4 flex flex-wrap items-center gap-3">
        <label className="text-sm text-slate-600">操作类型</label>
        <select
          value={action}
          onChange={(e) => {
            setAction(e.target.value);
            setPage(1);
          }}
          className="of-input max-w-[220px]"
        >
          <option value="">全部</option>
          {(data?.actions ?? []).map((a) => (
            <option key={a} value={a}>
              {ACTION_LABELS[a] || a}
            </option>
          ))}
        </select>
        <span className="text-xs text-slate-400">
          记录管理员的每一次敏感操作，便于事后追溯
        </span>
      </div>

      <SectionTitle count={data?.total}>操作记录</SectionTitle>
      <Card className="overflow-hidden p-0">
        {loading && !data ? (
          <Loading text="加载审计日志..." />
        ) : !data || data.items.length === 0 ? (
          <div className="p-6">
            <EmptyState
              title="暂无操作记录"
              description="管理员执行改角色、禁用账号、重置密码等操作后会记录在这里"
            />
          </div>
        ) : (
          <>
            <TableScroll>
              <table className="w-full min-w-[820px] text-left text-sm">
                <thead>
                  <tr className="border-b border-slate-100 bg-slate-50/80 text-slate-500">
                    <th className="px-5 py-3 font-medium">时间</th>
                    <th className="px-5 py-3 font-medium">操作者</th>
                    <th className="px-5 py-3 font-medium">操作</th>
                    <th className="px-5 py-3 font-medium">对象</th>
                    <th className="px-5 py-3 font-medium">变更详情</th>
                    <th className="px-5 py-3 font-medium">IP</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-slate-50">
                  {data.items.map((log) => (
                    <tr key={log.id} className="transition hover:bg-slate-50/50">
                      <td className="whitespace-nowrap px-5 py-3 text-xs text-slate-500">
                        {log.created_at ? new Date(`${log.created_at}Z`).toLocaleString("zh-CN") : "-"}
                      </td>
                      <td className="px-5 py-3 text-slate-700">
                        {log.operator_name || log.operator_id}
                      </td>
                      <td className="px-5 py-3">
                        <span className="rounded bg-brand-50 px-2 py-0.5 text-xs font-medium text-brand-700">
                          {ACTION_LABELS[log.action] || log.action}
                        </span>
                      </td>
                      <td className="px-5 py-3">
                        <div className="min-w-0">
                          <span className="text-slate-700">{log.target_label || "-"}</span>
                          {log.target_type && (
                            <span className="block text-[11px] text-slate-400">{log.target_type}</span>
                          )}
                        </div>
                      </td>
                      <td className="px-5 py-3 text-xs text-slate-600">{fmtDetail(log.detail)}</td>
                      <td className="px-5 py-3 font-mono text-xs text-slate-400">{log.ip || "-"}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </TableScroll>

            <div className="flex items-center justify-between border-t border-slate-100 px-5 py-3">
              <span className="text-xs text-slate-500">
                共 {data.total} 条 · 第 {data.page} 页
              </span>
              <div className="flex gap-2">
                <button
                  onClick={() => setPage((p) => Math.max(1, p - 1))}
                  disabled={page <= 1}
                  className="of-btn-ghost text-xs disabled:opacity-40"
                >
                  上一页
                </button>
                <button
                  onClick={() => setPage((p) => p + 1)}
                  disabled={page * data.page_size >= data.total}
                  className="of-btn-ghost text-xs disabled:opacity-40"
                >
                  下一页
                </button>
              </div>
            </div>
          </>
        )}
      </Card>
    </div>
  );
}
