import { useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { sendResetCode, resetPassword } from "../api/client";

const EMAIL_RE = /^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$/;

export default function ForgotPassword() {
  const [account, setAccount] = useState("");
  const [code, setCode] = useState("");
  const [password, setPassword] = useState("");
  const [confirmPassword, setConfirmPassword] = useState("");
  const [loading, setLoading] = useState(false);
  const [countdown, setCountdown] = useState(0);
  const [error, setError] = useState("");
  const [success, setSuccess] = useState("");
  const [sentHint, setSentHint] = useState("");
  const navigate = useNavigate();

  const handleSendCode = async () => {
    setError("");
    setSentHint("");
    const acc = account.trim();
    if (!EMAIL_RE.test(acc)) {
      setError("请输入正确的邮箱");
      return;
    }
    setLoading(true);
    try {
      const res = await sendResetCode(acc);
      if (res.success) {
        setSentHint(res.data?.email_masked ? `验证码已发送至 ${res.data.email_masked}` : "验证码已发送");
        setCountdown(60);
        const timer = window.setInterval(() => {
          setCountdown((c) => {
            if (c <= 1) { window.clearInterval(timer); return 0; }
            return c - 1;
          });
        }, 1000);
      } else {
        setError(res.error || "发送失败");
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : "发送失败，请检查网络");
    } finally {
      setLoading(false);
    }
  };

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError("");
    setSuccess("");
    const acc = account.trim();
    const targetEmail = acc;
    if (!EMAIL_RE.test(acc)) {
      setError("请输入正确的邮箱");
      return;
    }
    if (!code.trim()) { setError("请输入验证码"); return; }
    if (password.length < 6) { setError("新密码至少 6 位"); return; }
    if (password !== confirmPassword) { setError("两次密码输入不一致"); return; }

    setLoading(true);
    try {
      const res = await resetPassword(acc, targetEmail, code.trim(), password);
      if (res.success) {
        setSuccess("密码已重置，正在跳转到登录页...");
        setTimeout(() => navigate("/login"), 1500);
      } else {
        setError(res.error || "重置失败");
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : "重置失败，请检查网络");
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="relative min-h-screen overflow-hidden bg-gradient-to-br from-slate-950 via-brand-950 to-slate-900">
      <div className="absolute -top-40 -right-40 h-80 w-80 rounded-full bg-brand-600/20 blur-3xl" />
      <div className="absolute -bottom-40 -left-40 h-80 w-80 rounded-full bg-brand-500/15 blur-3xl" />

      <div className="relative mx-auto flex min-h-screen max-w-md items-center px-4">
        <div className="w-full rounded-3xl border border-white/10 bg-white/95 p-8 shadow-2xl backdrop-blur-xl sm:p-10">
          <div className="mb-8 text-center">
            <div className="mx-auto mb-5 flex h-16 w-16 items-center justify-center rounded-2xl bg-gradient-to-br from-brand-500 to-brand-700 text-xl font-black text-white shadow-lg shadow-brand-600/30">
              OF
            </div>
            <h1 className="text-2xl font-bold text-slate-950">找回密码</h1>
            <p className="mt-2 text-sm text-slate-500">通过邮箱验证码重置密码</p>
          </div>

          {error && <div className="of-alert-error mb-5">{error}</div>}
          {success && <div className="of-alert-success mb-5">{success}</div>}
          {sentHint && !success && <div className="of-alert-success mb-5">{sentHint}</div>}

          <form onSubmit={handleSubmit} className="space-y-5">
            <div>
              <label className="mb-2 block text-sm font-medium text-slate-700">邮箱</label>
              <input type="text" value={account} onChange={(e) => setAccount(e.target.value)}
                className="of-input" placeholder="请输入注册时的邮箱" disabled={loading} />
            </div>

            <div>
              <label className="mb-2 block text-sm font-medium text-slate-700">验证码</label>
              <div className="flex gap-2">
                <input type="text" value={code} onChange={(e) => setCode(e.target.value)}
                  className="of-input flex-1" placeholder="6 位验证码" maxLength={6} disabled={loading} />
                <button type="button" onClick={handleSendCode} disabled={loading || countdown > 0}
                  className="of-btn shrink-0 bg-brand-50 text-brand-700 hover:bg-brand-100 disabled:opacity-60">
                  {countdown > 0 ? `${countdown}s` : "发送验证码"}
                </button>
              </div>
            </div>

            <div>
              <label className="mb-2 block text-sm font-medium text-slate-700">新密码（至少 6 位）</label>
              <input type="password" value={password} onChange={(e) => setPassword(e.target.value)}
                className="of-input" placeholder="请输入新密码" disabled={loading} />
            </div>
            <div>
              <label className="mb-2 block text-sm font-medium text-slate-700">确认新密码</label>
              <input type="password" value={confirmPassword} onChange={(e) => setConfirmPassword(e.target.value)}
                className="of-input" placeholder="请再次输入新密码" disabled={loading} />
            </div>

            <button type="submit" disabled={loading}
              className="of-btn-primary h-11 w-full">
              {loading ? (
                <>
                  <span className="h-4 w-4 animate-spin rounded-full border-2 border-white/30 border-t-white" />
                  处理中...
                </>
              ) : "重置密码"}
            </button>
          </form>

          <p className="mt-6 text-center text-sm text-slate-500">
            想起密码了？
            <Link to="/login" className="font-medium text-brand-600 hover:text-brand-700">返回登录</Link>
          </p>
        </div>
      </div>
    </div>
  );
}
