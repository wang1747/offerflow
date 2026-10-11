import { useCallback, useEffect, useState } from "react";
import { api } from "../../api/client";
import type { AdminSettingsResult } from "../../types";
import { Card, Loading, SectionTitle, PageHeader, Icons } from "../../components/ui";

export default function AdminSettings() {
  const [data, setData] = useState<AdminSettingsResult | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");
  const [saving, setSaving] = useState("");

  // 各设置项的本地编辑态
  const [quota, setQuota] = useState("0");
  const [allowRegister, setAllowRegister] = useState(true);
  const [announcement, setAnnouncement] = useState("");
  const [announcementEnabled, setAnnouncementEnabled] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const res = await api.admin.settings();
      if (res.success && res.data) {
        setData(res.data);
        const s = res.data.settings;
        setQuota(String(s.quota_daily_tokens?.value ?? 0));
        setAllowRegister(Boolean(s.allow_register?.value));
        setAnnouncement(String(s.announcement?.value ?? ""));
        setAnnouncementEnabled(Boolean(s.announcement_enabled?.value));
      } else {
        setError(res.error || "加载失败");
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : "加载失败");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    const t = window.setTimeout(() => void load(), 0);
    return () => window.clearTimeout(t);
  }, [load]);

  const save = async (key: string, value: unknown, label: string) => {
    setSaving(key);
    setError("");
    setMessage("");
    try {
      const res = await api.admin.updateSetting(key, value);
      if (res.success) {
        setMessage(`${label}已保存`);
        await load();
      } else {
        setError(res.error || "保存失败");
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : "保存失败");
    } finally {
      setSaving("");
    }
  };

  if (loading && !data) return <Loading text="加载系统设置..." />;

  return (
    <div>
      <PageHeader title="系统设置" subtitle="配额、开关与公告" icon={Icons.settings} />

      {error && <div className="of-alert-error mb-4">{error}</div>}
      {message && <div className="of-alert-success mb-4">{message}</div>}

      {/* 平台默认模型（只读） */}
      <SectionTitle>平台默认模型</SectionTitle>
      <Card className="mb-5 p-5">
        <div className="grid grid-cols-1 gap-4 sm:grid-cols-3">
          <div>
            <p className="text-xs text-slate-500">当前模型</p>
            <p className="mt-1 font-mono text-sm font-semibold text-slate-900">
              {data?.platform_model.model || "-"}
            </p>
          </div>
          <div>
            <p className="text-xs text-slate-500">API Key</p>
            <p className={`mt-1 text-sm font-semibold ${data?.platform_model.key_configured ? "text-emerald-600" : "text-rose-600"}`}>
              {data?.platform_model.key_configured ? "已配置" : "未配置"}
            </p>
          </div>
          <div>
            <p className="text-xs text-slate-500">Base URL</p>
            <p className="mt-1 truncate text-sm text-slate-700">
              {data?.platform_model.base_url || "https://api.deepseek.com"}
            </p>
          </div>
        </div>
        <p className="mt-4 rounded-lg bg-slate-50 px-3 py-2 text-xs leading-relaxed text-slate-500">
          平台默认模型由服务器环境变量配置（<code className="rounded bg-white px-1">LLM_MODEL</code>），
          修改需更新服务器 <code className="rounded bg-white px-1">.env.prod</code> 并重启后端。
          未自备 Key 的用户会自动使用此模型，费用由平台承担。
        </p>
      </Card>

      {/* 用量配额 */}
      <SectionTitle>用量配额</SectionTitle>
      <Card className="mb-5 p-5">
        <p className="mb-3 text-xs leading-relaxed text-slate-500">
          {data?.settings.quota_daily_tokens?.description || "每用户每日 token 配额"}
        </p>
        <div className="flex flex-wrap items-end gap-3">
          <div>
            <label className="mb-1.5 block text-xs font-medium text-slate-600">每日上限（tokens）</label>
            <input
              type="number"
              min={0}
              value={quota}
              onChange={(e) => setQuota(e.target.value)}
              className="of-input max-w-[200px]"
            />
          </div>
          <button
            onClick={() => save("quota_daily_tokens", Number(quota) || 0, "配额")}
            disabled={saving === "quota_daily_tokens"}
            className="of-btn"
          >
            {saving === "quota_daily_tokens" ? "保存中..." : "保存"}
          </button>
          <span className="pb-2.5 text-xs text-slate-400">填 0 表示不限制</span>
        </div>
        <p className="mt-3 rounded-lg bg-amber-50 px-3 py-2 text-xs text-amber-800">
          配额只对「使用平台默认 Key」的用户生效；自带 Key 的用户花自己的额度，不受此限制。
        </p>
      </Card>

      {/* 注册开关 */}
      <SectionTitle>注册控制</SectionTitle>
      <Card className="mb-5 p-5">
        <div className="flex items-center justify-between gap-4">
          <div>
            <p className="text-sm font-medium text-slate-800">开放新用户注册</p>
            <p className="mt-0.5 text-xs text-slate-500">
              关闭后新用户将无法注册（已有账号不受影响），适合临时控制测试规模
            </p>
          </div>
          <button
            onClick={() => {
              const next = !allowRegister;
              setAllowRegister(next);
              void save("allow_register", next, "注册开关");
            }}
            disabled={saving === "allow_register"}
            className={`relative h-6 w-11 flex-shrink-0 rounded-full transition ${
              allowRegister ? "bg-emerald-500" : "bg-slate-300"
            }`}
          >
            <span
              className={`absolute top-0.5 h-5 w-5 rounded-full bg-white shadow transition-all ${
                allowRegister ? "left-[22px]" : "left-0.5"
              }`}
            />
          </button>
        </div>
      </Card>

      {/* 平台公告 */}
      <SectionTitle>平台公告</SectionTitle>
      <Card className="p-5">
        <div className="mb-3 flex items-center justify-between gap-4">
          <div>
            <p className="text-sm font-medium text-slate-800">向所有用户展示公告</p>
            <p className="mt-0.5 text-xs text-slate-500">开启后，用户在应用内会看到下面的公告内容</p>
          </div>
          <button
            onClick={() => {
              const next = !announcementEnabled;
              setAnnouncementEnabled(next);
              void save("announcement_enabled", next, "公告开关");
            }}
            disabled={saving === "announcement_enabled"}
            className={`relative h-6 w-11 flex-shrink-0 rounded-full transition ${
              announcementEnabled ? "bg-emerald-500" : "bg-slate-300"
            }`}
          >
            <span
              className={`absolute top-0.5 h-5 w-5 rounded-full bg-white shadow transition-all ${
                announcementEnabled ? "left-[22px]" : "left-0.5"
              }`}
            />
          </button>
        </div>

        <textarea
          value={announcement}
          onChange={(e) => setAnnouncement(e.target.value)}
          rows={4}
          placeholder="例如：平台将于本周日凌晨维护，期间 AI 功能可能短暂不可用。"
          className="of-input w-full resize-y"
        />
        <div className="mt-3 flex justify-end">
          <button
            onClick={() => save("announcement", announcement, "公告内容")}
            disabled={saving === "announcement"}
            className="of-btn"
          >
            {saving === "announcement" ? "保存中..." : "保存公告"}
          </button>
        </div>
      </Card>
    </div>
  );
}
