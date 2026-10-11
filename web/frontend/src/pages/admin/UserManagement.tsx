import { useCallback, useEffect, useState } from "react";
import { api } from "../../api/client";
import type { AdminUserPage, AdminUserRow, AdminUserDetail } from "../../types";
import { Card, EmptyState, Icons, Loading, Modal, PageHeader, TableScroll } from "../../components/ui";
import { formatCny } from "../../utils/currency";

const PAGE_SIZE = 20;

type Filter = "all" | "admin" | "disabled" | "byok";

const FILTERS: { key: Filter; label: string }[] = [
  { key: "all", label: "全部" },
  { key: "admin", label: "管理员" },
  { key: "disabled", label: "已禁用" },
  { key: "byok", label: "自带 Key" },
];

const fmtDate = (iso: string | null) =>
  iso ? new Date(`${iso}Z`).toLocaleDateString("zh-CN", { year: "numeric", month: "2-digit", day: "2-digit" }) : "-";

const fmtDateTime = (iso: string | null) =>
  iso ? new Date(`${iso}Z`).toLocaleString("zh-CN") : "-";

export default function UserManagement() {
  const [data, setData] = useState<AdminUserPage | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");

  const [q, setQ] = useState("");
  const [search, setSearch] = useState("");
  const [filter, setFilter] = useState<Filter>("all");
  const [page, setPage] = useState(1);

  // 详情弹窗
  const [detail, setDetail] = useState<AdminUserDetail | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);
  // 绑定邮箱弹窗
  const [emailUser, setEmailUser] = useState<AdminUserRow | null>(null);
  const [emailValue, setEmailValue] = useState("");
  // 重置密码弹窗
  const [pwdUser, setPwdUser] = useState<AdminUserRow | null>(null);
  const [pwdValue, setPwdValue] = useState("");
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const res = await api.admin.listUsers({
        q: search || undefined,
        role: filter === "admin" ? "admin" : undefined,
        is_active: filter === "disabled" ? false : undefined,
        byok: filter === "byok" ? true : undefined,
        page,
        page_size: PAGE_SIZE,
      });
      if (res.success && res.data) setData(res.data);
      else setError(res.error || "加载失败");
    } catch (err) {
      setError(err instanceof Error ? err.message : "加载失败");
    } finally {
      setLoading(false);
    }
  }, [search, filter, page]);

  useEffect(() => {
    const t = window.setTimeout(() => void load(), 0);
    return () => window.clearTimeout(t);
  }, [load]);

  const withAction = async (fn: () => Promise<{ success: boolean; error?: string | null }>, okText: string) => {
    setBusy(true);
    setError("");
    setMessage("");
    try {
      const res = await fn();
      if (res.success) {
        setMessage(okText);
        await load();
        return true;
      }
      setError(res.error || "操作失败");
    } catch (err) {
      setError(err instanceof Error ? err.message : "操作失败");
    } finally {
      setBusy(false);
    }
    return false;
  };

  const openDetail = async (u: AdminUserRow) => {
    setDetailLoading(true);
    setError("");
    try {
      const res = await api.admin.getUserDetail(u.id);
      if (res.success && res.data) setDetail(res.data);
      else setError(res.error || "加载详情失败");
    } catch (err) {
      setError(err instanceof Error ? err.message : "加载详情失败");
    } finally {
      setDetailLoading(false);
    }
  };

  const submitEmail = async () => {
    if (!emailUser) return;
    const ok = await withAction(
      () => api.admin.bindEmail(emailUser.id, emailValue.trim()),
      `已将 ${emailUser.name} 的邮箱绑定为 ${emailValue.trim()}`,
    );
    if (ok) {
      setEmailUser(null);
      setEmailValue("");
    }
  };

  const submitPassword = async () => {
    if (!pwdUser) return;
    if (pwdValue.length < 6) {
      setError("新密码至少 6 位");
      return;
    }
    const ok = await withAction(
      () => api.admin.resetPassword(pwdUser.id, pwdValue),
      `已重置 ${pwdUser.name} 的密码`,
    );
    if (ok) {
      setPwdUser(null);
      setPwdValue("");
    }
  };

  const exportCsv = async () => {
    setError("");
    try {
      const blob = await api.admin.exportUsers();
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = "offerflow_users.csv";
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
      URL.revokeObjectURL(url);
      setMessage("用户列表已导出");
    } catch (err) {
      setError(err instanceof Error ? err.message : "导出失败");
    }
  };

  const totalPages = data ? Math.max(1, Math.ceil(data.total / data.page_size)) : 1;

  return (
    <div>
      <PageHeader title="用户管理" subtitle="账号、角色与状态" icon={Icons.admin} />

      {error && <div className="of-alert-error mb-4">{error}</div>}
      {message && <div className="of-alert-success mb-4">{message}</div>}

      {/* 工具条 */}
      <div className="mb-4 flex flex-wrap items-center gap-3">
        <form
          onSubmit={(e) => {
            e.preventDefault();
            setPage(1);
            setSearch(q.trim());
          }}
          className="flex gap-2"
        >
          <input
            value={q}
            onChange={(e) => setQ(e.target.value)}
            placeholder="搜索邮箱 / 昵称 / ID"
            className="of-input w-56"
          />
          <button type="submit" className="of-btn">
            搜索
          </button>
          {search && (
            <button
              type="button"
              onClick={() => {
                setQ("");
                setSearch("");
                setPage(1);
              }}
              className="of-btn-ghost"
            >
              清除
            </button>
          )}
        </form>

        <div className="flex gap-1 rounded-xl bg-slate-100 p-1">
          {FILTERS.map((f) => (
            <button
              key={f.key}
              onClick={() => {
                setFilter(f.key);
                setPage(1);
              }}
              className={`rounded-lg px-3 py-1.5 text-xs font-medium transition ${
                filter === f.key ? "bg-white text-brand-600 shadow-sm" : "text-slate-500 hover:text-slate-700"
              }`}
            >
              {f.label}
            </button>
          ))}
        </div>

        <div className="ml-auto flex gap-2">
          <button onClick={exportCsv} className="of-btn-outline">
            导出 CSV
          </button>
          <button onClick={load} className="of-btn-ghost" disabled={loading}>
            刷新
          </button>
        </div>
      </div>

      <Card className="overflow-hidden p-0">
        {loading && !data ? (
          <Loading text="加载用户列表..." />
        ) : !data || data.items.length === 0 ? (
          <div className="p-6">
            <EmptyState title="没有匹配的用户" description="试试调整搜索词或筛选条件" />
          </div>
        ) : (
          <>
            <TableScroll>
              <table className="w-full min-w-[900px] text-left text-sm">
                <thead>
                  <tr className="border-b border-slate-100 bg-slate-50/80 text-slate-500">
                    <th className="px-5 py-3 font-medium">用户</th>
                    <th className="px-5 py-3 font-medium">角色</th>
                    <th className="px-5 py-3 font-medium">状态</th>
                    <th className="px-5 py-3 font-medium">模型来源</th>
                    <th className="px-5 py-3 text-right font-medium">调用 / 成本</th>
                    <th className="px-5 py-3 font-medium">注册时间</th>
                    <th className="px-5 py-3 text-right font-medium">操作</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-slate-50">
                  {data.items.map((u) => (
                    <tr key={u.id} className="transition hover:bg-slate-50/50">
                      <td className="px-5 py-3.5">
                        <div className="flex items-center gap-3">
                          <div
                            className={`flex h-8 w-8 flex-shrink-0 items-center justify-center rounded-full text-xs font-semibold ${
                              u.role === "admin" ? "bg-brand-100 text-brand-700" : "bg-slate-100 text-slate-500"
                            }`}
                          >
                            {u.name.charAt(0).toUpperCase()}
                          </div>
                          <div className="min-w-0">
                            <span className="block font-medium text-slate-900">{u.name}</span>
                            <span className="block truncate text-xs text-slate-400">
                              {u.email || <span className="text-rose-500">未绑定邮箱（无法登录）</span>}
                            </span>
                          </div>
                        </div>
                      </td>
                      <td className="px-5 py-3.5">
                        <select
                          value={u.role}
                          onChange={(e) =>
                            void withAction(
                              () => api.admin.updateRole(u.id, e.target.value as "admin" | "user"),
                              `${u.name} 角色已更新`,
                            )
                          }
                          className="cursor-pointer rounded-lg border border-slate-200 bg-white px-2.5 py-1 text-xs font-medium text-slate-600 outline-none transition hover:border-brand-400 focus:border-brand-500"
                        >
                          <option value="user">user</option>
                          <option value="admin">admin</option>
                        </select>
                      </td>
                      <td className="px-5 py-3.5">
                        <button
                          onClick={() =>
                            void withAction(
                              () => api.admin.updateActive(u.id, !u.is_active),
                              `${u.name} 已${u.is_active ? "禁用" : "启用"}`,
                            )
                          }
                          className={`of-badge cursor-pointer transition ${
                            u.is_active
                              ? "bg-emerald-50 text-emerald-600 hover:bg-emerald-100"
                              : "bg-rose-50 text-rose-600 hover:bg-rose-100"
                          }`}
                        >
                          <span className={`h-1.5 w-1.5 rounded-full ${u.is_active ? "bg-emerald-500" : "bg-rose-500"}`} />
                          {u.is_active ? "正常" : "禁用"}
                        </button>
                      </td>
                      <td className="px-5 py-3.5">
                        {u.byok ? (
                          <span className="of-badge bg-emerald-50 text-emerald-600">自带 Key</span>
                        ) : (
                          <span className="of-badge bg-amber-50 text-amber-700">平台额度</span>
                        )}
                      </td>
                      <td className="px-5 py-3.5 text-right">
                        <span className="block text-slate-700">{u.usage.calls}</span>
                        <span className="block text-xs text-slate-400">{formatCny(u.usage.cost_cny)}</span>
                      </td>
                      <td className="px-5 py-3.5 text-slate-500">{fmtDate(u.created_at)}</td>
                      <td className="px-5 py-3.5">
                        <div className="flex justify-end gap-1.5">
                          <button onClick={() => void openDetail(u)} className="of-btn-ghost text-xs">
                            详情
                          </button>
                          <button
                            onClick={() => {
                              setEmailUser(u);
                              setEmailValue(u.email || "");
                            }}
                            className="of-btn-ghost text-xs"
                          >
                            绑定邮箱
                          </button>
                          <button
                            onClick={() => {
                              setPwdUser(u);
                              setPwdValue("");
                            }}
                            className="of-btn-ghost text-xs"
                          >
                            重置密码
                          </button>
                        </div>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </TableScroll>

            <div className="flex items-center justify-between border-t border-slate-100 px-5 py-3">
              <span className="text-xs text-slate-500">
                共 {data.total} 位用户 · 第 {data.page}/{totalPages} 页
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
                  disabled={page >= totalPages}
                  className="of-btn-ghost text-xs disabled:opacity-40"
                >
                  下一页
                </button>
              </div>
            </div>
          </>
        )}
      </Card>

      {/* 详情弹窗 */}
      <Modal
        open={!!detail || detailLoading}
        onClose={() => setDetail(null)}
        title="用户详情"
        footer={
          <button onClick={() => setDetail(null)} className="of-btn-ghost">
            关闭
          </button>
        }
      >
        {detailLoading || !detail ? (
          <Loading text="加载中..." />
        ) : (
          <div className="space-y-4 text-sm">
            <div className="grid grid-cols-2 gap-3">
              <Info label="昵称" value={detail.user.name} />
              <Info label="邮箱" value={detail.user.email || "未绑定"} />
              <Info label="角色" value={detail.user.role} />
              <Info label="状态" value={detail.user.is_active ? "正常" : "已禁用"} />
              <Info label="模型来源" value={detail.user.byok ? "自带 Key" : "平台额度"} />
              <Info label="注册时间" value={fmtDateTime(detail.user.created_at)} />
            </div>

            <div>
              <p className="mb-2 text-xs font-semibold text-slate-500">用量</p>
              <div className="grid grid-cols-3 gap-2">
                <Info label="调用次数" value={String(detail.usage.calls)} />
                <Info label="Token 总量" value={detail.usage.tokens.toLocaleString()} />
                <Info label="成本" value={formatCny(detail.usage.cost_cny)} />
              </div>
              {detail.usage.quota_daily_tokens > 0 && (
                <p className="mt-2 text-xs text-slate-500">
                  今日用量 {detail.usage.today_tokens.toLocaleString()} /{" "}
                  {detail.usage.quota_daily_tokens.toLocaleString()}
                </p>
              )}
            </div>

            <div>
              <p className="mb-2 text-xs font-semibold text-slate-500">内容量</p>
              <div className="flex flex-wrap gap-2">
                {Object.entries(detail.content).map(([k, v]) => (
                  <span key={k} className="rounded-lg bg-slate-50 px-2.5 py-1 text-xs text-slate-600">
                    {CONTENT_LABELS[k] || k} {v}
                  </span>
                ))}
              </div>
            </div>

            {detail.top_operations.length > 0 && (
              <div>
                <p className="mb-2 text-xs font-semibold text-slate-500">最常用功能</p>
                <div className="space-y-1">
                  {detail.top_operations.map((o) => (
                    <div key={o.operation} className="flex justify-between text-xs">
                      <span className="font-mono text-slate-600">{o.operation}</span>
                      <span className="text-slate-500">
                        {o.calls} 次 · {formatCny(o.cost_cny)}
                      </span>
                    </div>
                  ))}
                </div>
              </div>
            )}
          </div>
        )}
      </Modal>

      {/* 绑定邮箱 */}
      <Modal
        open={!!emailUser}
        onClose={() => setEmailUser(null)}
        title={`为 ${emailUser?.name ?? ""} 绑定邮箱`}
        footer={
          <>
            <button onClick={() => setEmailUser(null)} className="of-btn-ghost">
              取消
            </button>
            <button onClick={submitEmail} className="of-btn" disabled={busy || !emailValue.trim()}>
              {busy ? "保存中..." : "保存"}
            </button>
          </>
        }
      >
        <p className="mb-3 text-xs leading-relaxed text-slate-500">
          邮箱即唯一身份，未绑定邮箱的账号无法登录。为目标账号绑定或更换邮箱。
        </p>
        <input
          value={emailValue}
          onChange={(e) => setEmailValue(e.target.value)}
          placeholder="user@example.com"
          className="of-input w-full"
        />
      </Modal>

      {/* 重置密码 */}
      <Modal
        open={!!pwdUser}
        onClose={() => setPwdUser(null)}
        title={`重置 ${pwdUser?.name ?? ""} 的密码`}
        footer={
          <>
            <button onClick={() => setPwdUser(null)} className="of-btn-ghost">
              取消
            </button>
            <button onClick={submitPassword} className="of-btn-danger" disabled={busy || pwdValue.length < 6}>
              {busy ? "重置中..." : "确认重置"}
            </button>
          </>
        }
      >
        <p className="mb-3 text-xs leading-relaxed text-slate-500">
          用于用户忘记密码时的恢复。重置后请通过其他渠道告知用户新密码。此操作会记入审计日志。
        </p>
        <input
          type="text"
          value={pwdValue}
          onChange={(e) => setPwdValue(e.target.value)}
          placeholder="新密码（至少 6 位）"
          className="of-input w-full"
        />
      </Modal>
    </div>
  );
}

function Info({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded-lg bg-slate-50 px-3 py-2">
      <p className="text-[11px] text-slate-500">{label}</p>
      <p className="mt-0.5 truncate font-medium text-slate-800">{value}</p>
    </div>
  );
}

const CONTENT_LABELS: Record<string, string> = {
  resumes: "简历",
  jds: "岗位",
  applications: "投递",
  interview_sessions: "面试",
  corpus: "语料",
  posts: "帖子",
  feedbacks: "反馈",
};
