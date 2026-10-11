import { useRef, useState } from "react";
import { api } from "../../api/client";
import { Card, PageHeader, Icons } from "../../components/ui";
import Avatar from "../../components/Avatar";
import { useAuthStore } from "../../store/authStore";

const inputCls =
  "w-full rounded-lg border border-slate-200 px-3 py-2 text-sm focus:border-brand-500 focus:outline-none focus:ring-1 focus:ring-brand-500";
const btnPrimary =
  "rounded-lg bg-brand-600 px-4 py-2 text-sm font-medium text-white transition hover:bg-brand-700 disabled:opacity-50";

export default function AccountSecurity() {
  const { user, setUser } = useAuthStore();
  const isAdmin = user?.role === "admin";
  const [name, setName] = useState(user?.name || "");
  const [nameMsg, setNameMsg] = useState("");
  const [nameSaving, setNameSaving] = useState(false);
  const [uploadingAvatar, setUploadingAvatar] = useState(false);
  const avatarInputRef = useRef<HTMLInputElement>(null);

  const [oldPassword, setOldPassword] = useState("");
  const [newPassword, setNewPassword] = useState("");
  const [confirmPassword, setConfirmPassword] = useState("");
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");
  const [saving, setSaving] = useState(false);

  const saveName = async () => {
    const trimmed = name.trim();
    setError("");
    setNameMsg("");
    if (!trimmed) {
      setError("昵称不能为空");
      return;
    }
    if (trimmed === user?.name) {
      setNameMsg("昵称未变化");
      return;
    }
    setNameSaving(true);
    try {
      await api.auth.updateProfile(trimmed);
      if (user) setUser({ ...user, name: trimmed });
      setNameMsg("昵称已更新");
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setNameSaving(false);
    }
  };

  const onAvatarChange = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    if (!file) return;
    setUploadingAvatar(true);
    setError("");
    try {
      const res = await api.auth.uploadAvatar(file);
      if (res.success && res.data) {
        if (user) setUser({ ...user, avatar: res.data.avatar ?? null });
        setNameMsg("头像已更新");
      } else {
        setError(res.error || "上传失败");
      }
    } catch (ex) {
      setError(ex instanceof Error ? ex.message : "上传失败");
    } finally {
      setUploadingAvatar(false);
      e.target.value = "";
    }
  };

  const submit = async () => {
    setError("");
    setMessage("");

    if (!oldPassword) {
      setError("请输入当前密码");
      return;
    }
    if (newPassword.length < 6) {
      setError("新密码至少 6 位");
      return;
    }
    if (newPassword !== confirmPassword) {
      setError("两次输入的新密码不一致");
      return;
    }

    setSaving(true);
    try {
      await api.auth.changePassword(oldPassword, newPassword);
      setMessage("密码已修改，下次登录请使用新密码");
      setOldPassword("");
      setNewPassword("");
      setConfirmPassword("");
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setSaving(false);
    }
  };

  return (
    <div>
      {isAdmin && (
        <PageHeader title="账号安全" subtitle="昵称与登录密码" icon={Icons.settings} />
      )}

      {isAdmin && (
        <Card className="mb-4 max-w-xl p-6">
          <h3 className="text-base font-semibold text-slate-900">个人资料</h3>
          <p className="mt-1 text-sm text-slate-500">修改你的头像与显示昵称。</p>

          {nameMsg && (
            <div className="mt-4 flex items-center justify-between rounded-lg bg-emerald-50 px-4 py-2 text-sm text-emerald-600">
              <span>{nameMsg}</span>
              <button className="text-emerald-400 hover:text-emerald-600" onClick={() => setNameMsg("")}>×</button>
            </div>
          )}

          <div className="mt-5 space-y-4">
            <div className="flex items-center gap-4">
              <Avatar name={user?.name || "U"} src={user?.avatar} size={64} />
              <div>
                <input
                  ref={avatarInputRef}
                  type="file"
                  accept=".jpg,.jpeg,.png,.gif,.webp"
                  className="hidden"
                  onChange={onAvatarChange}
                />
                <button
                  className="rounded-lg border border-slate-200 px-3 py-1.5 text-sm font-medium text-slate-600 transition hover:bg-slate-50"
                  onClick={() => avatarInputRef.current?.click()}
                  disabled={uploadingAvatar}
                >
                  {uploadingAvatar ? "上传中…" : "更换头像"}
                </button>
                <p className="mt-1 text-xs text-slate-400">支持 jpg/png/gif/webp，超过 2MB 会自动压缩</p>
              </div>
            </div>
            <div>
              <label className="mb-1 block text-sm text-slate-600">昵称</label>
              <input className={inputCls} value={name} onChange={(e) => setName(e.target.value)} maxLength={32} />
            </div>
            <button className={btnPrimary} onClick={saveName} disabled={nameSaving}>
              {nameSaving ? "保存中…" : "保存昵称"}
            </button>
          </div>
        </Card>
      )}

      <Card className="max-w-xl p-6">
      <h3 className="text-base font-semibold text-slate-900">修改登录密码</h3>
      <p className="mt-1 text-sm text-slate-500">
        密码经 bcrypt 加密存储，不会以明文保存。修改后当前会话仍有效，下次登录需使用新密码。
      </p>

      {error && (
        <div className="mt-4 flex items-center justify-between rounded-lg bg-rose-50 px-4 py-2 text-sm text-rose-600">
          <span>{error}</span>
          <button className="text-rose-400 hover:text-rose-600" onClick={() => setError("")}>×</button>
        </div>
      )}
      {message && (
        <div className="mt-4 flex items-center justify-between rounded-lg bg-emerald-50 px-4 py-2 text-sm text-emerald-600">
          <span>{message}</span>
          <button className="text-emerald-400 hover:text-emerald-600" onClick={() => setMessage("")}>×</button>
        </div>
      )}

      <div className="mt-5 space-y-4">
        <div>
          <label className="mb-1 block text-sm text-slate-600">当前密码</label>
          <input
            type="password"
            className={inputCls}
            value={oldPassword}
            onChange={(e) => setOldPassword(e.target.value)}
            autoComplete="current-password"
          />
        </div>
        <div>
          <label className="mb-1 block text-sm text-slate-600">新密码（至少 6 位）</label>
          <input
            type="password"
            className={inputCls}
            value={newPassword}
            onChange={(e) => setNewPassword(e.target.value)}
            autoComplete="new-password"
          />
        </div>
        <div>
          <label className="mb-1 block text-sm text-slate-600">确认新密码</label>
          <input
            type="password"
            className={inputCls}
            value={confirmPassword}
            onChange={(e) => setConfirmPassword(e.target.value)}
            autoComplete="new-password"
          />
        </div>
        <button className={btnPrimary} onClick={submit} disabled={saving}>
          {saving ? "保存中…" : "修改密码"}
        </button>
      </div>
      </Card>
    </div>
  );
}
