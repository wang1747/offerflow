import { useCallback, useEffect, useState } from "react";
import { api } from "../../api/client";
import type { Feedback } from "../../types";
import { Badge, Card, EmptyState, Loading, Modal, TableScroll } from "../../components/ui";

const FEEDBACK_CATEGORIES = ["功能建议", "问题反馈", "体验优化", "其他"];

const FEEDBACK_STATUS: Record<string, { label: string; color: "gray" | "blue" | "green" | "amber" | "red" }> = {
  pending: { label: "待处理", color: "amber" },
  accepted: { label: "已采纳", color: "green" },
  declined: { label: "已婉拒", color: "gray" },
  replied: { label: "已回复", color: "blue" },
};

const STATUS_KEYS = ["pending", "accepted", "declined", "replied"];

const formatDate = (iso?: string) => {
  if (!iso) return "-";
  const d = new Date(iso);
  return `${d.toLocaleDateString()} ${d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}`;
};

const btnPrimary =
  "rounded-lg bg-brand-600 px-4 py-2 text-sm font-medium text-white transition hover:bg-brand-700 disabled:opacity-50";
const btnGhost =
  "rounded-lg border border-slate-200 px-4 py-2 text-sm font-medium text-slate-600 transition hover:bg-slate-50";
const inputCls =
  "w-full rounded-lg border border-slate-200 px-3 py-2 text-sm focus:border-brand-500 focus:outline-none focus:ring-1 focus:ring-brand-500";

export default function FeedbackManage() {
  const [items, setItems] = useState<Feedback[]>([]);
  const [statusFilter, setStatusFilter] = useState("");
  const [categoryFilter, setCategoryFilter] = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");

  const [editing, setEditing] = useState<Feedback | null>(null);
  const [editStatus, setEditStatus] = useState("replied");
  const [replyText, setReplyText] = useState("");
  const [saving, setSaving] = useState(false);

  const load = useCallback(async (status = statusFilter, category = categoryFilter) => {
    setLoading(true);
    setError("");
    try {
      const res = await api.feedback.list(category || undefined, 1, 100, status || undefined);
      if (res.success && res.data) setItems(res.data.items);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setLoading(false);
    }
  }, [statusFilter, categoryFilter]);

  useEffect(() => {
    void load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const openEdit = (f: Feedback) => {
    setEditing(f);
    setEditStatus(f.status === "pending" ? "replied" : f.status);
    setReplyText(f.admin_reply || "");
    setMessage("");
  };

  const save = async () => {
    if (!editing) return;
    setSaving(true);
    setError("");
    try {
      await api.feedback.adminUpdate(editing.id, {
        status: editStatus || undefined,
        admin_reply: replyText.trim() || undefined,
      });
      setMessage("已保存处理结果");
      setEditing(null);
      await load();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setSaving(false);
    }
  };

  return (
    <div>
      {error && (
        <div className="mb-4 flex items-center justify-between rounded-lg bg-rose-50 px-4 py-2 text-sm text-rose-600">
          <span>{error}</span>
          <button className="text-rose-400 hover:text-rose-600" onClick={() => setError("")}>×</button>
        </div>
      )}
      {message && (
        <div className="mb-4 flex items-center justify-between rounded-lg bg-emerald-50 px-4 py-2 text-sm text-emerald-600">
          <span>{message}</span>
          <button className="text-emerald-400 hover:text-emerald-600" onClick={() => setMessage("")}>×</button>
        </div>
      )}

      <div className="mb-4 flex flex-wrap items-center justify-between gap-3">
        <div className="flex flex-wrap items-center gap-2">
          <span className="mr-1 text-xs text-slate-400">状态</span>
          <button className={`${statusFilter === "" ? btnPrimary : btnGhost}`}
            onClick={() => { setStatusFilter(""); load("", categoryFilter); }}>
            全部
          </button>
          {STATUS_KEYS.map((s) => (
            <button key={s} className={`${statusFilter === s ? btnPrimary : btnGhost}`}
              onClick={() => { setStatusFilter(s); load(s, categoryFilter); }}>
              {FEEDBACK_STATUS[s].label}
            </button>
          ))}
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <span className="mr-1 text-xs text-slate-400">分类</span>
          <button className={`${categoryFilter === "" ? btnPrimary : btnGhost}`}
            onClick={() => { setCategoryFilter(""); load(statusFilter, ""); }}>
            全部
          </button>
          {FEEDBACK_CATEGORIES.map((c) => (
            <button key={c} className={`${categoryFilter === c ? btnPrimary : btnGhost}`}
              onClick={() => { setCategoryFilter(c); load(statusFilter, c); }}>
              {c}
            </button>
          ))}
        </div>
      </div>

      <Card className="overflow-hidden p-0">
        {loading ? (
          <Loading text="加载建议列表..." />
        ) : items.length === 0 ? (
          <div className="p-6">
            <EmptyState title="暂无建议" description="用户还没有提交建议" />
          </div>
        ) : (
          <TableScroll>
            <table className="w-full min-w-[720px] text-left text-sm">
              <thead>
                <tr className="border-b border-slate-100 bg-slate-50/80 text-slate-500">
                  <th className="px-5 py-3 font-medium">建议</th>
                  <th className="px-5 py-3 font-medium">作者</th>
                  <th className="px-5 py-3 font-medium">点赞</th>
                  <th className="px-5 py-3 font-medium">时间</th>
                  <th className="px-5 py-3 text-right font-medium">操作</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-50">
                {items.map((f) => {
                  const st = FEEDBACK_STATUS[f.status] || FEEDBACK_STATUS.pending;
                  return (
                    <tr key={f.id} className="transition hover:bg-slate-50/50">
                      <td className="px-5 py-3.5">
                        <div className="flex items-center gap-2">
                          <Badge color="indigo">{f.category}</Badge>
                          <Badge color={st.color}>{st.label}</Badge>
                        </div>
                        <span className="mt-1 block font-medium text-slate-900">{f.title}</span>
                      </td>
                      <td className="px-5 py-3.5 text-slate-600">{f.author}</td>
                      <td className="px-5 py-3.5 text-slate-500">♥ {f.like_count}</td>
                      <td className="px-5 py-3.5 text-slate-500">{formatDate(f.created_at)}</td>
                      <td className="px-5 py-3.5 text-right">
                        <button className="of-btn-ghost !px-3 !py-1.5 text-xs" onClick={() => openEdit(f)}>
                          处理
                        </button>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </TableScroll>
        )}
      </Card>

      <Modal
        open={!!editing}
        onClose={() => setEditing(null)}
        title="处理建议"
        footer={
          <>
            <button className={btnGhost} onClick={() => setEditing(null)}>取消</button>
            <button className={btnPrimary} onClick={save} disabled={saving}>保存</button>
          </>
        }
      >
        {editing && (
          <div className="space-y-4">
            <div className="flex flex-wrap items-center gap-2 text-xs text-slate-400">
              <Badge color="indigo">{editing.category}</Badge>
              <span>{editing.author}</span>
              <span>{formatDate(editing.created_at)}</span>
            </div>
            <div>
              <h3 className="text-base font-semibold text-slate-900">{editing.title}</h3>
              <p className="mt-2 whitespace-pre-wrap text-sm leading-6 text-slate-600">{editing.content}</p>
            </div>
            <div>
              <label className="mb-1 block text-sm text-slate-600">状态</label>
              <select className={inputCls} value={editStatus} onChange={(e) => setEditStatus(e.target.value)}>
                {STATUS_KEYS.map((s) => (
                  <option key={s} value={s}>{FEEDBACK_STATUS[s].label}</option>
                ))}
              </select>
            </div>
            <div>
              <label className="mb-1 block text-sm text-slate-600">官方回复</label>
              <textarea className={`${inputCls} min-h-28`} placeholder="回复会公开显示在建议下方…"
                value={replyText} onChange={(e) => setReplyText(e.target.value)} />
            </div>
          </div>
        )}
      </Modal>
    </div>
  );
}
