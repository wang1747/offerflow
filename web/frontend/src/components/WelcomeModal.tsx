import { useEffect, useState } from "react";
import { Icons } from "./ui";

const STORAGE_KEY = "offerflow_welcome_seen_v1";

export default function WelcomeModal() {
  const [open, setOpen] = useState(false);

  useEffect(() => {
    try {
      if (!localStorage.getItem(STORAGE_KEY)) setOpen(true);
    } catch {
      /* 忽略 localStorage 不可用 */
    }
  }, []);

  const close = () => {
    try {
      localStorage.setItem(STORAGE_KEY, "1");
    } catch {
      /* 忽略 */
    }
    setOpen(false);
  };

  if (!open) return null;

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/45 p-4"
      role="dialog"
      aria-modal="true"
      aria-label="欢迎使用 OfferFlow"
    >
      <div className="w-full max-w-lg overflow-hidden rounded-2xl bg-white shadow-2xl animate-fade-in">
        <div className="flex items-center gap-3 border-b border-slate-100 px-6 py-4">
          <div className="flex h-10 w-10 items-center justify-center rounded-xl bg-brand-600 text-sm font-black text-white">
            OF
          </div>
          <div>
            <h2 className="text-base font-semibold text-slate-900">欢迎使用 OfferFlow</h2>
            <p className="text-xs text-slate-400">你的 AI 求职助手</p>
          </div>
        </div>

        <div className="space-y-4 px-6 py-5 text-sm leading-relaxed text-slate-600">
          <div>
            <h3 className="mb-1 flex items-center gap-1.5 text-sm font-medium text-slate-800">
              {Icons.rocket} 关于项目
            </h3>
            <p>
              OfferFlow 是一款 AI 驱动的求职工具，帮你一键生成匹配目标岗位的专业简历、分析岗位要求、模拟面试、进行薪资谈判，让求职准备更高效、更有底气。
            </p>
          </div>

          <div>
            <h3 className="mb-1 flex items-center gap-1.5 text-sm font-medium text-slate-800">
              {Icons.alert} 数据共享说明
            </h3>
            <p>
              为了持续提升服务质量，你使用过程中产生的简历、岗位描述、面试内容等，会经过脱敏处理后用于优化 AI 的生成与检索能力，让推荐越来越懂你。我们不会公开或售卖你的任何个人信息。
            </p>
          </div>

          <div className="rounded-xl bg-brand-50/60 px-4 py-3 text-brand-800">
            感谢你的信任与使用。愿 OfferFlow 陪你走好求职的每一步，祝你早日拿下心仪的 offer。
          </div>
        </div>

        <div className="border-t border-slate-100 px-6 py-4">
          <button onClick={close} className="of-btn-primary w-full py-2.5">
            开始使用
          </button>
        </div>
      </div>
    </div>
  );
}
