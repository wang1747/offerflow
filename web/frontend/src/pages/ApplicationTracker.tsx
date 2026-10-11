import { type FC, useCallback, useEffect, useState } from "react";
import { api } from "../api/client";
import { onSynced } from "../offline";
import KanbanBoard from "../components/KanbanBoard";
import StatsDashboard from "../components/StatsDashboard";
import { STATUS_LABELS, STATUS_ORDER } from "../constants/application";
import type { Application, ApplicationStatus, JDItem, Reminders, ResumeItem } from "../types";
import { Card, PageHeader, SectionTitle, Modal, Badge, Icons } from "../components/ui";
import { resumeLabel } from "../utils/resume";

interface EditForm {
  status: string; applied_date: string; next_action: string;
  next_action_date: string; notes: string;
}

const ApplicationTracker: FC = () => {
  const [items, setItems] = useState<Application[]>([]);
  const [reminders, setReminders] = useState<Reminders | null>(null);
  const [jds, setJds] = useState<JDItem[]>([]);
  const [resumes, setResumes] = useState<ResumeItem[]>([]);

  const [company, setCompany] = useState("");
  const [position, setPosition] = useState("");
  const [jdId, setJdId] = useState("");
  const [resumeId, setResumeId] = useState("");

  const [editing, setEditing] = useState<Application | null>(null);
  const [editForm, setEditForm] = useState<EditForm>({
    status: "saved", applied_date: "", next_action: "", next_action_date: "", notes: "",
  });

  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");

  const loadAll = useCallback(async () => {
    try {
      const [appsRes, remindRes, jdRes, resumeRes] = await Promise.all([
        api.applications.list(), api.applications.reminders(), api.jd.list(), api.resume.list(),
      ]);
      if (appsRes.success && appsRes.data) setItems(appsRes.data);
      if (remindRes.success && remindRes.data) setReminders(remindRes.data);
      if (jdRes.success && jdRes.data) setJds(jdRes.data);
      if (resumeRes.success && resumeRes.data) setResumes(resumeRes.data);
    } catch (err) {
      setError(err instanceof Error ? err.message : "加载失败");
    }
  }, []);

  useEffect(() => { void loadAll(); }, [loadAll]);

  // 离线期间存的改动被后台同步回服务器后，本地列表要跟着换成服务端的真实数据
  useEffect(() => onSynced(() => { void loadAll(); }), [loadAll]);

  const handleCreate = async () => {
    setError(""); setMessage("");
    if (!company.trim() || !position.trim()) { setError("请填写公司和职位"); return; }
    setSaving(true);
    try {
      const res = await api.applications.create(company.trim(), position.trim(), jdId || undefined, resumeId || undefined);
      if (!res.success) { setError(res.error || "创建失败"); return; }
      setCompany(""); setPosition(""); setJdId(""); setResumeId("");
      setMessage("已添加投递记录");
      await loadAll();
    } catch (err) {
      setError(err instanceof Error ? err.message : "创建失败");
    } finally {
      setSaving(false);
    }
  };

  const handleStatusChange = async (app: Application, status: ApplicationStatus) => {
    if (app.status === status) return;
    setError(""); setMessage("");
    try {
      const res = await api.applications.updateStatus(app.id, status);
      if (res.success) { setMessage(`已更新为「${STATUS_LABELS[status]}」`); await loadAll(); }
      else setError(res.error || "更新失败");
    } catch (err) {
      setError(err instanceof Error ? err.message : "更新失败");
    }
  };

  const openEdit = (app: Application) => {
    setEditing(app);
    setEditForm({
      status: app.status, applied_date: app.applied_date || "",
      next_action: app.next_action || "", next_action_date: app.next_action_date || "",
      notes: app.notes || "",
    });
  };

  const handleSaveEdit = async () => {
    if (!editing) return;
    setError(""); setMessage(""); setSaving(true);
    try {
      const res = await api.applications.update(editing.id, {
        status: editForm.status, applied_date: editForm.applied_date || undefined,
        next_action: editForm.next_action || undefined, next_action_date: editForm.next_action_date || undefined,
        notes: editForm.notes || undefined,
      });
      if (res.success) { setEditing(null); setMessage("已保存"); await loadAll(); }
      else setError(res.error || "保存失败");
    } catch (err) {
      setError(err instanceof Error ? err.message : "保存失败");
    } finally {
      setSaving(false);
    }
  };

  const handleDelete = async (app: Application) => {
    if (!window.confirm(`确定删除 ${app.company} 的投递记录？`)) return;
    setError(""); setMessage("");
    try {
      await api.applications.remove(app.id);
      setMessage("已删除");
      await loadAll();
    } catch (err) {
      setError(err instanceof Error ? err.message : "删除失败");
    }
  };

  return (
    <div>
      <PageHeader title="投递追踪" subtitle="管理你的投递进度，拖拽看板随时更新状态" icon={Icons.track} />

      {error && <div className="of-alert-error mb-4">{Icons.alert}<span>{error}</span></div>}
      {message && <div className="of-alert-success mb-4">{Icons.check}<span>{message}</span></div>}

      <div className="mb-5"><StatsDashboard /></div>

      {reminders && reminders.has_reminders && (
        <div className="mb-5 grid gap-4 lg:grid-cols-2">
          <Card className="border-rose-200 bg-rose-50/30 p-4">
            <h2 className="mb-3 flex items-center gap-2 text-sm font-semibold text-rose-700">
              <span className="h-2 w-2 rounded-full bg-rose-500" />
              超期未跟进 ({reminders.overdue_count})
            </h2>
            {reminders.overdue.length === 0 ? (
              <p className="text-sm text-slate-400">暂无</p>
            ) : (
              <ul className="space-y-2">
                {reminders.overdue.map((item) => (
                  <li key={item.id} className="flex justify-between gap-3 text-sm">
                    <span className="min-w-0 truncate text-slate-700">{item.company} · {item.position}</span>
                    <Badge color="red">{item.days_since_update} 天</Badge>
                  </li>
                ))}
              </ul>
            )}
          </Card>
          <Card className="border-blue-200 bg-blue-50/30 p-4">
            <h2 className="mb-3 flex items-center gap-2 text-sm font-semibold text-blue-700">
              <span className="h-2 w-2 rounded-full bg-blue-500" />
              即将到来的面试 ({reminders.upcoming_count})
            </h2>
            {reminders.upcoming.length === 0 ? (
              <p className="text-sm text-slate-400">暂无</p>
            ) : (
              <ul className="space-y-2">
                {reminders.upcoming.map((item) => (
                  <li key={item.id} className="flex justify-between gap-3 text-sm">
                    <span className="min-w-0 truncate text-slate-700">
                      {item.company} · {item.position}
                      {item.next_action ? ` · ${item.next_action}` : ""}
                    </span>
                    <span className="flex-shrink-0 text-slate-400">{item.next_action_date}</span>
                  </li>
                ))}
              </ul>
            )}
          </Card>
        </div>
      )}

      <Card className="mb-5 p-5">
        <SectionTitle>添加投递记录</SectionTitle>
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-5">
          <input value={company} onChange={(e) => setCompany(e.target.value)} placeholder="公司名称" className="of-input" aria-label="公司名称" />
          <input value={position} onChange={(e) => setPosition(e.target.value)} placeholder="职位" className="of-input" aria-label="职位" />
          <select value={jdId} onChange={(e) => setJdId(e.target.value)} className="of-select" aria-label="关联岗位">
            <option value="">关联岗位（可选）</option>
            {jds.map((jd) => (
              <option key={jd.id} value={jd.id}>{jd.company || "未知公司"} · {jd.position || "未知职位"}</option>
            ))}
          </select>
          <select value={resumeId} onChange={(e) => setResumeId(e.target.value)} className="of-select" aria-label="关联简历">
            <option value="">关联简历（可选）</option>
            {resumes.map((resume) => (
              <option key={resume.id} value={resume.id}>{resumeLabel(resume)}</option>
            ))}
          </select>
          <button onClick={handleCreate} disabled={saving} className="of-btn-primary">
            {saving ? "添加中..." : "添加"}
          </button>
        </div>
      </Card>

      <Card className="p-5">
        <SectionTitle>进度看板</SectionTitle>
        <KanbanBoard items={items} onStatusChange={handleStatusChange} onEdit={openEdit} onDelete={handleDelete} />
      </Card>

      <Modal
        open={!!editing}
        onClose={() => setEditing(null)}
        title="编辑投递记录"
        footer={
          <>
            <button onClick={() => setEditing(null)} className="of-btn-ghost">取消</button>
            <button onClick={handleSaveEdit} disabled={saving} className="of-btn-primary">
              {saving ? "保存中..." : "保存"}
            </button>
          </>
        }
      >
        {editing && (
          <div>
            <div className="mb-4 rounded-xl bg-slate-50 px-4 py-2.5 text-sm text-slate-600">
              {editing.company} · {editing.position}
            </div>
            <div className="space-y-3">
              <div>
                <label className="mb-1 block text-sm text-slate-600">状态</label>
                <select value={editForm.status} onChange={(e) => setEditForm({ ...editForm, status: e.target.value })} className="of-select">
                  {STATUS_ORDER.map((status) => (<option key={status} value={status}>{STATUS_LABELS[status]}</option>))}
                </select>
              </div>
              <div>
                <label className="mb-1 block text-sm text-slate-600">投递日期</label>
                <input type="date" value={editForm.applied_date} onChange={(e) => setEditForm({ ...editForm, applied_date: e.target.value })} className="of-input" />
              </div>
              <div>
                <label className="mb-1 block text-sm text-slate-600">下一步行动</label>
                <input value={editForm.next_action} onChange={(e) => setEditForm({ ...editForm, next_action: e.target.value })} className="of-input" placeholder="例如：准备技术面" />
              </div>
              <div>
                <label className="mb-1 block text-sm text-slate-600">提醒日期</label>
                <input type="date" value={editForm.next_action_date} onChange={(e) => setEditForm({ ...editForm, next_action_date: e.target.value })} className="of-input" />
              </div>
              <div>
                <label className="mb-1 block text-sm text-slate-600">备注</label>
                <textarea value={editForm.notes} onChange={(e) => setEditForm({ ...editForm, notes: e.target.value })} className="of-textarea h-24 resize-none" />
              </div>
            </div>
          </div>
        )}
      </Modal>
    </div>
  );
};

export default ApplicationTracker;
