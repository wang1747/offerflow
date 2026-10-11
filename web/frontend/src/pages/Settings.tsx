import { useEffect, useRef, useState, type ReactNode } from "react";
import { useSearchParams } from "react-router-dom";
import { api } from "../api/client";
import { useAuthStore } from "../store/authStore";
import { Card, PageHeader, Tabs, Icons } from "../components/ui";
import Avatar from "../components/Avatar";
import { applyTheme, getTheme, type Theme } from "../utils/theme";
import type { User } from "../types";
import AccountSecurity from "./admin/AccountSecurity";
import ModelSettings from "./ModelSettings";

const inputCls =
  "w-full rounded-lg border border-slate-200 px-3 py-2 text-sm focus:border-brand-500 focus:outline-none focus:ring-1 focus:ring-brand-500";
const btnPrimary =
  "rounded-lg bg-brand-600 px-4 py-2 text-sm font-medium text-white transition hover:bg-brand-700 disabled:opacity-50";

// ===================== 个人资料 =====================
function EmailField({
  value,
  onUpdated,
  onError,
  onMsg,
}: {
  value?: string;
  onUpdated: (user: User) => void;
  onError: (msg: string) => void;
  onMsg: (msg: string) => void;
}) {
  const [editing, setEditing] = useState(false);
  const [newEmail, setNewEmail] = useState("");
  const [code, setCode] = useState("");
  const [sent, setSent] = useState(false);
  const [cooldown, setCooldown] = useState(0);
  const [sending, setSending] = useState(false);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    if (cooldown <= 0) return;
    const t = window.setTimeout(() => setCooldown((c) => c - 1), 1000);
    return () => window.clearTimeout(t);
  }, [cooldown]);

  const close = () => {
    setEditing(false);
    setNewEmail("");
    setCode("");
    setSent(false);
    setCooldown(0);
  };

  const sendCode = async () => {
    const v = newEmail.trim().toLowerCase();
    onError("");
    onMsg("");
    if (!/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(v)) {
      onError("请输入正确的邮箱");
      return;
    }
    if (v === (value || "").trim().toLowerCase()) {
      onError("新邮箱与当前一致");
      return;
    }
    setSending(true);
    try {
      const res = await api.auth.sendChangeEmailCode(v);
      setSent(true);
      setCooldown(60);
      onMsg(res.data?.email_masked ? `验证码已发送至 ${res.data.email_masked}` : "验证码已发送");
    } catch (e) {
      onError((e as Error).message);
    } finally {
      setSending(false);
    }
  };

  const save = async () => {
    const v = newEmail.trim().toLowerCase();
    onError("");
    onMsg("");
    if (!v) {
      onError("请输入新的邮箱");
      return;
    }
    if (!code.trim()) {
      onError("请输入邮箱验证码");
      return;
    }
    setBusy(true);
    try {
      const res = await api.auth.changeEmail(v, code.trim());
      if (res.data) onUpdated(res.data);
      close();
      onMsg("邮箱已换绑，请使用新邮箱登录");
    } catch (e) {
      onError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div>
      <label className="mb-1 block text-sm text-slate-600">登录邮箱</label>
      <div className="flex items-center gap-2">
        <div className="flex flex-1 items-center gap-2 truncate rounded-lg border border-slate-100 bg-slate-50 px-3 py-2 text-sm text-slate-500">
          <span className="truncate">{value || "未绑定"}</span>
          {value && (
            <span className="shrink-0 rounded bg-emerald-50 px-1.5 py-0.5 text-xs text-emerald-600">
              已绑定
            </span>
          )}
        </div>
        <button
          onClick={() => (editing ? close() : setEditing(true))}
          className="shrink-0 rounded-lg border border-slate-200 px-3 py-2 text-sm text-slate-600 transition hover:border-brand-300 hover:text-brand-600"
        >
          {editing ? "取消" : "换绑"}
        </button>
      </div>
      <p className="mt-1 text-xs text-slate-400">
        邮箱是唯一登录账号，注册后即绑定。换绑需通过新邮箱验证码验证。
      </p>
      {editing && (
        <div className="mt-2 space-y-2 rounded-lg border border-slate-100 bg-slate-50/60 p-3">
          <input
            type="email"
            className={inputCls}
            placeholder="请输入新邮箱"
            value={newEmail}
            onChange={(e) => {
              setNewEmail(e.target.value);
              setSent(false);
            }}
            autoComplete="email"
          />
          {sent && (
            <input
              className={inputCls}
              placeholder="请输入 6 位邮箱验证码"
              value={code}
              onChange={(e) => setCode(e.target.value)}
              maxLength={6}
              autoComplete="one-time-code"
            />
          )}
          <div className="flex gap-2">
            {!sent ? (
              <button
                onClick={sendCode}
                disabled={sending || cooldown > 0}
                className="rounded-lg bg-brand-600 px-3 py-1.5 text-sm font-medium text-white transition hover:bg-brand-700 disabled:opacity-50"
              >
                {sending ? "发送中…" : cooldown > 0 ? `${cooldown}s 后可重发` : "发送验证码"}
              </button>
            ) : (
              <button
                onClick={save}
                disabled={busy}
                className="rounded-lg bg-brand-600 px-3 py-1.5 text-sm font-medium text-white transition hover:bg-brand-700 disabled:opacity-50"
              >
                {busy ? "提交中…" : "确认换绑"}
              </button>
            )}
            {sent && (
              <button
                onClick={sendCode}
                disabled={sending || cooldown > 0}
                className="rounded-lg border border-slate-200 px-3 py-1.5 text-sm text-slate-600 hover:bg-slate-100 disabled:opacity-50"
              >
                {cooldown > 0 ? `${cooldown}s 后重发` : "重新发送"}
              </button>
            )}
            <button
              onClick={close}
              className="rounded-lg border border-slate-200 px-3 py-1.5 text-sm text-slate-600 hover:bg-slate-100"
            >
              取消
            </button>
          </div>
        </div>
      )}
    </div>
  );
}

function ProfileTab() {
  const { user, setUser } = useAuthStore();
  const [name, setName] = useState(user?.name || "");
  const [saving, setSaving] = useState(false);
  const [msg, setMsg] = useState("");
  const [err, setErr] = useState("");
  const [uploadingAvatar, setUploadingAvatar] = useState(false);
  const avatarInputRef = useRef<HTMLInputElement>(null);

  const onUserUpdated = (u: User) => {
    if (user) setUser({ ...user, name: u.name, email: u.email });
  };

  const onAvatarChange = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    if (!file) return;
    setUploadingAvatar(true);
    setErr("");
    setMsg("");
    try {
      const res = await api.auth.uploadAvatar(file);
      if (res.success && res.data) {
        if (user) setUser({ ...user, avatar: res.data.avatar ?? null });
        setMsg("头像已更新");
      } else {
        setErr(res.error || "上传失败");
      }
    } catch (ex) {
      setErr(ex instanceof Error ? ex.message : "上传失败");
    } finally {
      setUploadingAvatar(false);
      e.target.value = "";
    }
  };

  const save = async () => {
    const trimmed = name.trim();
    setErr("");
    setMsg("");
    if (!trimmed) {
      setErr("昵称不能为空");
      return;
    }
    if (trimmed === user?.name) {
      setMsg("昵称未变化");
      return;
    }
    setSaving(true);
    try {
      await api.auth.updateProfile(trimmed);
      if (user) setUser({ ...user, name: trimmed });
      setMsg("昵称已更新");
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setSaving(false);
    }
  };

  return (
    <Card className="max-w-xl p-6">
      <h3 className="text-base font-semibold text-slate-900">个人资料</h3>
      <p className="mt-1 text-sm text-slate-500">管理你的昵称与登录邮箱。邮箱是唯一登录账号，换绑需通过新邮箱验证码验证。</p>

      {err && (
        <div className="mt-4 flex items-center justify-between rounded-lg bg-rose-50 px-4 py-2 text-sm text-rose-600">
          <span>{err}</span>
          <button className="text-rose-400 hover:text-rose-600" onClick={() => setErr("")}>×</button>
        </div>
      )}
      {msg && (
        <div className="mt-4 flex items-center justify-between rounded-lg bg-emerald-50 px-4 py-2 text-sm text-emerald-600">
          <span>{msg}</span>
          <button className="text-emerald-400 hover:text-emerald-600" onClick={() => setMsg("")}>×</button>
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
        <EmailField value={user?.email} onUpdated={onUserUpdated} onError={setErr} onMsg={setMsg} />
        <button className={btnPrimary} onClick={save} disabled={saving}>
          {saving ? "保存中…" : "保存昵称"}
        </button>
      </div>
    </Card>
  );
}

// ===================== 数据与隐私（暂时隐藏入口，代码保留待恢复）=====================
export function PrivacyTab() {
  const [allow, setAllow] = useState(false);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [msg, setMsg] = useState("");
  const [err, setErr] = useState("");

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const res = await api.corpus.getConsent();
        if (!cancelled && res.success && res.data) setAllow(res.data.allow_corpus);
      } catch (e) {
        if (!cancelled) setErr((e as Error).message);
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  const toggle = async () => {
    const next = !allow;
    setSaving(true);
    setErr("");
    setMsg("");
    try {
      await api.corpus.setConsent(next);
      setAllow(next);
      setMsg(next ? "已开启：你的简历/JD/面经将脱敏后贡献到共享语料库" : "已关闭：你的内容不再贡献到共享语料库");
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setSaving(false);
    }
  };

  return (
    <Card className="max-w-xl p-6">
      <h3 className="text-base font-semibold text-slate-900">数据与隐私</h3>
      <p className="mt-1 text-sm text-slate-500">控制你的数据是否参与共享，用于改善大家的求职体验。</p>

      {err && (
        <div className="mt-4 flex items-center justify-between rounded-lg bg-rose-50 px-4 py-2 text-sm text-rose-600">
          <span>{err}</span>
          <button className="text-rose-400 hover:text-rose-600" onClick={() => setErr("")}>×</button>
        </div>
      )}
      {msg && (
        <div className="mt-4 flex items-center justify-between rounded-lg bg-emerald-50 px-4 py-2 text-sm text-emerald-600">
          <span>{msg}</span>
          <button className="text-emerald-400 hover:text-emerald-600" onClick={() => setMsg("")}>×</button>
        </div>
      )}

      <div className="mt-5 flex items-center justify-between rounded-xl border border-slate-100 px-4 py-3">
        <div>
          <p className="text-sm font-medium text-slate-900">贡献语料到共享库</p>
          <p className="mt-0.5 text-xs text-slate-500">开启后，你的简历/JD/面经会脱敏处理，用于提升检索质量</p>
        </div>
        <button
          role="switch"
          aria-checked={allow}
          disabled={loading || saving}
          onClick={toggle}
          className={`relative h-6 w-11 shrink-0 rounded-full transition-colors disabled:opacity-50 ${
            allow ? "bg-brand-600" : "bg-slate-300"
          }`}
        >
          <span
            className={`absolute top-0.5 h-5 w-5 rounded-full bg-white shadow transition-transform ${
              allow ? "translate-x-5" : "translate-x-0.5"
            }`}
          />
        </button>
      </div>
    </Card>
  );
}

// ===================== 外观 =====================
function AppearanceTab() {
  const [theme, setTheme] = useState<Theme>(getTheme());

  const choose = (t: Theme) => {
    applyTheme(t);
    setTheme(t);
  };

  const options: { key: Theme; label: string; desc: string }[] = [
    { key: "light", label: "浅色", desc: "明亮清爽，适合白天" },
    { key: "dark", label: "深色", desc: "护眼，适合夜间使用" },
  ];

  return (
    <Card className="max-w-xl p-6">
      <h3 className="text-base font-semibold text-slate-900">外观</h3>
      <p className="mt-1 text-sm text-slate-500">选择界面主题，偏好会保存在本地浏览器。</p>

      <div className="mt-5 grid grid-cols-2 gap-3">
        {options.map((o) => (
          <button
            key={o.key}
            onClick={() => choose(o.key)}
            className={`rounded-xl border p-4 text-left transition ${
              theme === o.key
                ? "border-brand-500 bg-brand-50 ring-2 ring-brand-100"
                : "border-slate-200 hover:border-brand-300"
            }`}
          >
            <p className={`text-sm font-medium ${theme === o.key ? "text-brand-700" : "text-slate-900"}`}>
              {o.label}
            </p>
            <p className="mt-0.5 text-xs text-slate-500">{o.desc}</p>
          </button>
        ))}
      </div>
    </Card>
  );
}

// ===================== 设置中心 =====================
type SettingsTab = "general" | "model";

const TABS: { key: SettingsTab; label: string; icon: ReactNode }[] = [
  { key: "general", label: "账号与偏好", icon: Icons.settings },
  { key: "model", label: "AI 模型", icon: Icons.sparkle },
];

export default function Settings() {
  const [searchParams] = useSearchParams();
  const [tab, setTab] = useState<SettingsTab>(
    searchParams.get("tab") === "model" ? "model" : "general",
  );

  return (
    <div>
      <PageHeader
        title="设置"
        subtitle="账号信息、安全与偏好"
        icon={Icons.settings}
      />

      <Tabs tabs={TABS} active={tab} onChange={(k) => setTab(k as SettingsTab)} />

      {tab === "general" && (
        <div className="grid grid-cols-1 gap-4 xl:grid-cols-2">
          <div className="space-y-4">
            <ProfileTab />
            {/* 数据与隐私：暂时隐藏（所有数据默认共享，见登录欢迎说明） */}
          </div>
          <div className="space-y-4">
            <AccountSecurity />
            <AppearanceTab />
          </div>
        </div>
      )}
      {tab === "model" && <ModelSettings />}
    </div>
  );
}
