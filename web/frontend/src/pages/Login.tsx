import { useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { loginRequest } from "../api/client";
import { useAuthStore } from "../store/authStore";

export default function Login() {
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const navigate = useNavigate();
  const { login, setUser } = useAuthStore();

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!email.trim() || !password.trim()) {
      setError("请输入邮箱和密码");
      return;
    }
    setLoading(true);
    setError("");
    try {
      const response = await loginRequest(email.trim(), password.trim());
      if (response.success && response.data) {
        login(response.data.access_token);
        // 登录响应里已带完整用户信息（含昵称），立即写入 store，
        // 否则要等刷新页面触发 initialize() 才会显示正确昵称
        setUser(response.data.user);
        navigate("/");
      } else {
        setError(response.error || "登录失败");
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : "登录失败，请检查网络连接");
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="relative min-h-screen overflow-hidden bg-gradient-to-br from-slate-950 via-brand-950 to-slate-900">
      {/* Decorative blobs */}
      <div className="absolute -top-40 -right-40 h-80 w-80 rounded-full bg-brand-600/20 blur-3xl" />
      <div className="absolute -bottom-40 -left-40 h-80 w-80 rounded-full bg-brand-500/15 blur-3xl" />

      <div className="relative mx-auto flex min-h-screen max-w-md items-center px-4">
        <div className="w-full rounded-3xl border border-white/10 bg-white/95 p-8 shadow-2xl backdrop-blur-xl sm:p-10">
          <div className="mb-8 text-center">
            <div className="mx-auto mb-5 flex h-16 w-16 items-center justify-center rounded-2xl bg-gradient-to-br from-brand-500 to-brand-700 text-xl font-black text-white shadow-lg shadow-brand-600/30">
              OF
            </div>
            <h1 className="text-2xl font-bold text-slate-950">OfferFlow</h1>
            <p className="mt-2 text-sm text-slate-500">求职全流程智能 Agent 系统</p>
          </div>

          {error && (
            <div className="of-alert-error mb-5">
              <span>{error}</span>
            </div>
          )}

          <form onSubmit={handleSubmit} className="space-y-5">
            <div>
              <label className="mb-2 block text-sm font-medium text-slate-700">邮箱</label>
              <input
                type="email"
                value={email}
                onChange={(e) => setEmail(e.target.value)}
                className="of-input"
                placeholder="请输入注册邮箱"
                autoComplete="email"
                disabled={loading}
              />
            </div>
            <div>
              <div className="mb-2 flex items-center justify-between">
                <label className="block text-sm font-medium text-slate-700">密码</label>
                <Link to="/forgot-password" className="text-xs font-medium text-brand-600 hover:text-brand-700">
                  忘记密码？
                </Link>
              </div>
              <input
                type="password"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                className="of-input"
                placeholder="请输入密码"
                disabled={loading}
              />
            </div>
            <button
              type="submit"
              disabled={loading}
              className="of-btn-primary h-11 w-full"
            >
              {loading ? (
                <>
                  <span className="h-4 w-4 animate-spin rounded-full border-2 border-white/30 border-t-white" />
                  登录中...
                </>
              ) : "登录"}
            </button>
          </form>

          <p className="mt-6 text-center text-sm text-slate-500">
            还没有账号？
            <Link to="/register" className="font-medium text-brand-600 hover:text-brand-700">
              去注册
            </Link>
          </p>

          <p className="mt-3 text-center text-sm text-slate-400">
            想装到电脑或手机上？
            <Link
              to="/download"
              className="font-medium text-brand-600 hover:text-brand-700"
            >
              下载客户端
            </Link>
          </p>
        </div>
      </div>
    </div>
  );
}
