import { type ReactNode, useEffect, useState } from "react";
import { useLocation } from "react-router-dom";
import Sidebar from "./Sidebar";
import WelcomeModal from "./WelcomeModal";
import OfflineBanner from "./OfflineBanner";
import { Icons } from "./ui";

export default function Layout({ children }: { children: ReactNode }) {
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const location = useLocation();

  // 路由切换后自动收起移动端抽屉
  useEffect(() => {
    setSidebarOpen(false);
  }, [location.pathname]);

  return (
    <div className="min-h-screen bg-slate-50">
      <Sidebar open={sidebarOpen} onClose={() => setSidebarOpen(false)} />
      <WelcomeModal />

      {/* 移动端顶栏（lg 以下显示） */}
      <header className="of-mobile-header fixed inset-x-0 top-0 z-20 flex items-center gap-3 border-b border-slate-200 bg-white/95 px-4 backdrop-blur lg:hidden">
        <button
          onClick={() => setSidebarOpen(true)}
          className="flex h-9 w-9 items-center justify-center rounded-lg text-slate-600 transition hover:bg-slate-100"
          aria-label="打开菜单"
        >
          {Icons.menu}
        </button>
        <div className="flex h-8 w-8 items-center justify-center rounded-lg bg-gradient-to-br from-brand-400 to-brand-600 text-xs font-black text-white">
          OF
        </div>
        <span className="text-sm font-bold text-slate-900">OfferFlow</span>
      </header>

      <main className="min-h-screen lg:ml-64">
        {/* 移动端顶栏占位（避免内容被遮挡）——高度需与顶栏一致（含安全区） */}
        <div className="of-mobile-header-spacer lg:hidden" />
        <div className="of-safe-bottom mx-auto max-w-7xl px-4 py-6 animate-fade-in sm:px-6 sm:py-8">
          <OfflineBanner />
          {children}
        </div>
      </main>
    </div>
  );
}
