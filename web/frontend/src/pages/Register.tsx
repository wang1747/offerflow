import { useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { registerRequest } from "../api/client";

const EMAIL_RE = /^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$/;

export default function Register() {
  const [name, setName] = useState("");
  const [account, setAccount] = useState("");
  const [password, setPassword] = useState("");
  const [confirmPassword, setConfirmPassword] = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [success, setSuccess] = useState("");
  const navigate = useNavigate();

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError("");
    setSuccess("");
    const acc = account.trim();
    if (!acc || !password.trim()) { setError("请输入邮箱和密码"); return; }
    if (!EMAIL_RE.test(acc)) {
      setError("请输入正确的邮箱地址");
      return;
    }
    if (password.length < 6) { setError("密码至少 6 位"); return; }
    if (password !== confirmPassword) { setError("两次密码输入不一致"); return; }

    setLoading(true);
    try {
      const response = await registerRequest(acc, name.trim(), password.trim());
      if (response.success) {
        setSuccess("注册成功，正在跳转到登录页...");
        setTimeout(() => navigate("/login"), 1200);
      } else {
        setError(response.error || "注册失败");
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : "注册失败，请检查网络连接");
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="relative min-h-screen overflow-hidden bg-gradient-to-br from-slate-950 via-emerald-950 to-slate-900">
      <div className="absolute -top-40 -right-40 h-80 w-80 rounded-full bg-emerald-600/20 blur-3xl" />
      <div className="absolute -bottom-40 -left-40 h-80 w-80 rounded-full bg-emerald-500/15 blur-3xl" />

      <div className="relative mx-auto flex min-h-screen max-w-md items-center px-4">
        <div className="w-full rounded-3xl border border-white/10 bg-white/95 p-8 shadow-2xl backdrop-blur-xl sm:p-10">
          <div className="mb-8 text-center">
            <div className="mx-auto mb-5 flex h-16 w-16 items-center justify-center rounded-2xl bg-gradient-to-br from-emerald-500 to-emerald-700 text-xl font-black text-white shadow-lg shadow-emerald-600/30">
              OF
            </div>
            <h1 className="text-2xl font-bold text-slate-950">注册账号</h1>
            <p className="mt-2 text-sm text-slate-500">创建你的 OfferFlow 工作台</p>
          </div>

          {error && <div className="of-alert-error mb-5">{error}</div>}
          {success && <div className="of-alert-success mb-5">{success}</div>}

          <form onSubmit={handleSubmit} className="space-y-5">
            <div>
              <label className="mb-2 block text-sm font-medium text-slate-700">昵称（称呼）</label>
              <input type="text" value={name} onChange={(e) => setName(e.target.value)}
                className="of-input" placeholder="怎么称呼你（选填，留空自动生成）" disabled={loading} />
            </div>
            <div>
              <label className="mb-2 block text-sm font-medium text-slate-700">邮箱</label>
              <input type="text" value={account} onChange={(e) => setAccount(e.target.value)}
                className="of-input" placeholder="请输入邮箱" disabled={loading} />
            </div>
            <div>
              <label className="mb-2 block text-sm font-medium text-slate-700">密码（至少 6 位）</label>
              <input type="password" value={password} onChange={(e) => setPassword(e.target.value)}
                className="of-input" placeholder="请输入密码" disabled={loading} />
            </div>
            <div>
              <label className="mb-2 block text-sm font-medium text-slate-700">确认密码</label>
              <input type="password" value={confirmPassword} onChange={(e) => setConfirmPassword(e.target.value)}
                className="of-input" placeholder="请再次输入密码" disabled={loading} />
            </div>
            <button type="submit" disabled={loading}
              className="of-btn h-11 w-full bg-emerald-600 text-white shadow-soft hover:bg-emerald-700 active:scale-[0.98] disabled:opacity-50">
              {loading ? (
                <>
                  <span className="h-4 w-4 animate-spin rounded-full border-2 border-white/30 border-t-white" />
                  注册中...
                </>
              ) : "注册"}
            </button>
          </form>

          <p className="mt-6 text-center text-sm text-slate-500">
            已有账号？
            <Link to="/login" className="font-medium text-emerald-600 hover:text-emerald-700">去登录</Link>
          </p>
        </div>
      </div>
    </div>
  );
}
