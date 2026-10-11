import { useState, useEffect } from "react";
import { useNavigate } from "react-router-dom";
import { useAuthStore } from "../store/authStore";
import { api } from "../api/client";
import { Card, Badge, Loading } from "../components/ui";

interface PresetProvider {
  key: string;
  base_url: string;
  model: string;
}

interface ModelConfig {
  provider: string;
  base_url: string;
  model: string;
  api_key_masked: string;
  has_config: boolean;
  using_default: boolean;
}

export default function ModelSettings() {
  const { token } = useAuthStore();
  const navigate = useNavigate();

  const [presets, setPresets] = useState<PresetProvider[]>([]);
  const [config, setConfig] = useState<ModelConfig | null>(null);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [testing, setTesting] = useState(false);

  const [selectedProvider, setSelectedProvider] = useState("");
  const [baseUrl, setBaseUrl] = useState("");
  const [model, setModel] = useState("");
  const [apiKey, setApiKey] = useState("");

  const [error, setError] = useState("");
  const [success, setSuccess] = useState("");
  const [testResult, setTestResult] = useState<{ success: boolean; message?: string } | null>(null);

  const loadData = async () => {
    try {
      const [presetsRes, configRes] = await Promise.allSettled([
        api.modelConfig.presets(),
        api.modelConfig.get(),
      ]);
      if (presetsRes.status === "fulfilled" && presetsRes.value.success && presetsRes.value.data) {
        setPresets(presetsRes.value.data);
      }
      if (configRes.status === "fulfilled" && configRes.value.success && configRes.value.data) {
        setConfig(configRes.value.data);
        if (configRes.value.data.has_config) {
          setSelectedProvider(configRes.value.data.provider);
          setBaseUrl(configRes.value.data.base_url);
          setModel(configRes.value.data.model);
        }
      } else if (configRes.status === "rejected") {
        setError("加载当前模型配置失败");
      }
    } catch {
      setError("加载配置失败");
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    if (!token) {
      navigate("/login");
      return;
    }
    const timer = window.setTimeout(() => {
      void loadData();
    }, 0);
    return () => window.clearTimeout(timer);
  }, [navigate, token]);

  const handlePresetChange = (key: string) => {
    const preset = presets.find((p) => p.key === key);
    if (preset) {
      setSelectedProvider(key);
      setBaseUrl(preset.base_url);
      setModel(preset.model);
    }
  };

  const handleTest = async () => {
    if (!baseUrl || !model || !apiKey) {
      setTestResult({ success: false, message: "请填写完整配置" });
      return;
    }
    setTesting(true);
    setTestResult(null);
    try {
      const res = await api.modelConfig.test({
        base_url: baseUrl,
        model: model,
        api_key: apiKey,
      });
      if (res.success) {
        setTestResult({ success: true, message: "连接成功！模型可用" });
      } else {
        setTestResult({ success: false, message: res.error || "连接失败" });
      }
    } catch (err) {
      setTestResult({
        success: false,
        message: err instanceof Error ? err.message : "测试失败",
      });
    } finally {
      setTesting(false);
    }
  };

  const handleSave = async () => {
    if (!selectedProvider || !baseUrl || !model || !apiKey) {
      setError("请填写完整配置");
      return;
    }
    if (apiKey.length < 8) {
      setError("API Key 长度至少8位");
      return;
    }
    setSaving(true);
    setError("");
    setSuccess("");
    try {
      const res = await api.modelConfig.save({
        provider: selectedProvider,
        base_url: baseUrl,
        model: model,
        api_key: apiKey,
      });
      if (res.success) {
        setSuccess("配置保存成功！2 秒后跳转首页...");
        await loadData();
        setApiKey("");
        setTimeout(() => navigate("/"), 2000);
      } else {
        setError(res.error || "保存失败");
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : "保存失败");
    } finally {
      setSaving(false);
    }
  };

  const handleClear = async () => {
    if (!window.confirm("确定要清除模型配置吗？清除后 LLM 功能将不可用。")) return;
    try {
      const res = await api.modelConfig.clear();
      if (res.success) {
        setSuccess("配置已清除");
        setConfig(null);
        setSelectedProvider("");
        setBaseUrl("");
        setModel("");
        setApiKey("");
        setTestResult(null);
      } else {
        setError(res.error || "清除失败");
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : "清除失败");
    }
  };

  // 数据管理（导出/删除全部）已暂时隐藏，代码见 git 历史，需要时恢复

  if (loading) {
    return <Loading text="加载模型配置..." />;
  }

  return (
    <div>
      {/* 当前配置状态 */}
      {config?.has_config && (
        <Card className="mb-4 border-emerald-200 bg-emerald-50/50 p-4">
          <div className="flex items-start gap-3">
            <div className="flex h-9 w-9 flex-shrink-0 items-center justify-center rounded-lg bg-emerald-100 text-emerald-600">
              <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                <path d="M20 6L9 17l-5-5" />
              </svg>
            </div>
            <div className="min-w-0 flex-1">
              <div className="flex items-center gap-2">
                <p className="text-sm font-semibold text-emerald-800">当前已配置</p>
                <Badge color="green">{config.provider}</Badge>
                <Badge color="blue">{config.model}</Badge>
              </div>
              <p className="mt-1 text-xs text-slate-500">
                API Key: <code className="rounded bg-emerald-100 px-1.5 py-0.5 text-emerald-700">{config.api_key_masked}</code>
              </p>
            </div>
          </div>
        </Card>
      )}

      {/* 使用平台默认模型 */}
      {config?.using_default && (
        <Card className="mb-4 border-sky-200 bg-sky-50/50 p-4">
          <div className="flex items-start gap-3">
            <div className="flex h-9 w-9 flex-shrink-0 items-center justify-center rounded-lg bg-sky-100 text-sky-600">
              <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
                <circle cx="12" cy="12" r="10" />
                <path d="M12 16v-4M12 8h.01" />
              </svg>
            </div>
            <div className="min-w-0 flex-1">
              <p className="text-sm font-semibold text-sky-800">正在使用平台默认模型</p>
              <p className="mt-1 text-xs leading-relaxed text-slate-600">
                当前未配置自有 Key，平台已提供默认模型供你使用。默认 Key 仅存服务端、对外不可见；如需使用自己的 Key，请在下方填写。
              </p>
            </div>
          </div>
        </Card>
      )}

      {/* 密钥说明 */}
      <Card className="mb-4 border-blue-100 bg-blue-50/30 p-4">
        <div className="flex items-start gap-3">
          <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" className="mt-0.5 flex-shrink-0 text-blue-500">
            <circle cx="12" cy="12" r="10" />
            <path d="M12 16v-4M12 8h.01" />
          </svg>
          <p className="text-xs leading-relaxed text-slate-600">
            你的密钥仅加密存储在服务端，只用于调用你指定的 AI 模型。平台不代付费用，成本完全由你自己掌控。
          </p>
        </div>
      </Card>

      {error && (
        <div className="of-alert-error">
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" className="mt-0.5 flex-shrink-0">
            <circle cx="12" cy="12" r="10" />
            <path d="M12 8v4M12 16h.01" />
          </svg>
          {error}
        </div>
      )}

      {success && (
        <div className="of-alert-success">
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" className="mt-0.5 flex-shrink-0">
            <path d="M20 6L9 17l-5-5" />
          </svg>
          {success}
        </div>
      )}

      <Card className="p-6">
        <div className="space-y-5">
          {/* 提供商预设 */}
          <div>
            <label className="mb-2 block text-sm font-medium text-slate-700">
              模型提供商
              <span className="ml-1.5 text-xs font-normal text-slate-400">选择预设自动填充</span>
            </label>
            <div className="flex flex-wrap gap-2">
              {presets.map((p) => (
                <button
                  key={p.key}
                  onClick={() => handlePresetChange(p.key)}
                  className={`rounded-xl border px-4 py-2 text-sm font-medium transition-all duration-200 ${
                    selectedProvider === p.key
                      ? "border-brand-500 bg-brand-50 text-brand-700 shadow-soft"
                      : "border-slate-200 bg-white text-slate-600 hover:border-slate-300"
                  }`}
                >
                  {p.key.charAt(0).toUpperCase() + p.key.slice(1)}
                </button>
              ))}
            </div>
          </div>

          <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
            <div>
              <label className="mb-2 block text-sm font-medium text-slate-700">Base URL</label>
              <input
                type="url"
                value={baseUrl}
                onChange={(e) => setBaseUrl(e.target.value)}
                placeholder="https://api.deepseek.com"
                className="of-input"
              />
            </div>
            <div>
              <label className="mb-2 block text-sm font-medium text-slate-700">模型名</label>
              <input
                type="text"
                value={model}
                onChange={(e) => setModel(e.target.value)}
                placeholder="deepseek-flash"
                className="of-input"
              />
            </div>
          </div>

          <div>
            <label className="mb-2 block text-sm font-medium text-slate-700">
              API Key
              {config?.has_config && (
                <span className="ml-2 text-xs font-normal text-slate-400">
                  当前掩码: <code className="rounded bg-slate-100 px-1.5 py-0.5 text-slate-500">{config.api_key_masked}</code>
                </span>
              )}
            </label>
            <div className="relative">
              <input
                type="password"
                value={apiKey}
                onChange={(e) => setApiKey(e.target.value)}
                placeholder="输入新的 API Key（将覆盖旧配置）"
                className="of-input pr-10"
              />
              <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" className="absolute right-3 top-1/2 -translate-y-1/2 text-slate-400">
                <rect x="3" y="11" width="18" height="11" rx="2" />
                <path d="M7 11V7a5 5 0 0 1 10 0v4" />
              </svg>
            </div>
            {apiKey.length > 0 && apiKey.length < 8 && (
              <p className="mt-1.5 text-xs text-rose-500">API Key 长度至少 8 位</p>
            )}
          </div>

          <div className="flex flex-wrap gap-3 border-t border-slate-100 pt-5">
            <button
              onClick={handleTest}
              disabled={testing || !baseUrl || !model || !apiKey}
              className="of-btn-outline"
            >
              {testing ? (
                <>
                  <div className="h-4 w-4 animate-spin rounded-full border-2 border-slate-300 border-t-brand-500" />
                  测试中...
                </>
              ) : (
                <>
                  <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
                    <path d="M22 11.08V12a10 10 0 1 1-5.93-9.14" />
                    <path d="M22 4L12 14.01l-3-3" />
                  </svg>
                  测试连接
                </>
              )}
            </button>
            <button
              onClick={handleSave}
              disabled={saving || !selectedProvider || !baseUrl || !model || !apiKey}
              className="of-btn-primary"
            >
              {saving ? (
                <>
                  <div className="h-4 w-4 animate-spin rounded-full border-2 border-white/30 border-t-white" />
                  保存中...
                </>
              ) : (
                "保存配置"
              )}
            </button>
            {config?.has_config && (
              <button onClick={handleClear} className="of-btn-danger">
                <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
                  <path d="M3 6h18M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2" />
                </svg>
                清除配置
              </button>
            )}
          </div>

          {testResult && (
            <div
              className={`flex items-center gap-2.5 rounded-xl border px-4 py-3 text-sm ${
                testResult.success
                  ? "border-emerald-200 bg-emerald-50 text-emerald-700"
                  : "border-rose-200 bg-rose-50 text-rose-700"
              }`}
            >
              {testResult.success ? (
                <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                  <path d="M20 6L9 17l-5-5" />
                </svg>
              ) : (
                <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                  <circle cx="12" cy="12" r="10" />
                  <path d="M15 9l-6 6M9 9l6 6" />
                </svg>
              )}
              {testResult.message}
            </div>
          )}
        </div>
      </Card>

      {/* 数据管理：暂时隐藏（数据默认共享，说明见登录欢迎弹窗） */}
    </div>
  );
}
