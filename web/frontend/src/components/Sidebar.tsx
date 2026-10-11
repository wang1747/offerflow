import { Link, NavLink, useNavigate } from "react-router-dom";
import { useAuthStore } from "../store/authStore";
import { Icons } from "./ui";
import Avatar from "./Avatar";
import {
  WORKSPACE_MODULES,
  SYSTEM_MODULES,
  ADMIN_MODULES,
  MODULES,
  type AppModule,
} from "../modules";

function NavItem({ module, onClose }: { module: AppModule; onClose?: () => void }) {
  return (
    <NavLink
      to={module.path}
      end={module.end}
      onClick={onClose}
      className={({ isActive }) =>
        `group flex items-center gap-3 rounded-xl px-3 py-2.5 text-sm font-medium transition-all duration-200 ${
          isActive
            ? "bg-brand-50 text-brand-700"
            : "text-slate-600 hover:bg-slate-100 hover:text-slate-900"
        }`
      }
    >
      <span className="flex h-5 w-5 flex-shrink-0 items-center justify-center [&>svg]:h-full [&>svg]:w-full">{module.icon}</span>
      <span className="min-w-0 flex-1">
        <span className="block truncate leading-tight">{module.label}</span>
        <span className="block truncate text-[10px] font-normal leading-tight text-slate-400">
          {module.subtitle}
        </span>
      </span>
    </NavLink>
  );
}

export default function Sidebar({ open, onClose }: { open: boolean; onClose: () => void }) {
  const { user, logout } = useAuthStore();
  const navigate = useNavigate();

  const handleLogout = () => {
    onClose();
    logout();
    navigate("/login");
  };

  const isAdmin = user?.role === "admin";
  // 管理菜单的子分组（如「治理」「运维」），动态从模块注册表提取
  const adminGroups = Array.from(
    new Set(ADMIN_MODULES.map((m) => m.group).filter((g): g is string => Boolean(g))),
  );

  return (
    <>
      {/* 移动端遮罩 */}
      {open && (
        <div
          className="fixed inset-0 z-30 bg-slate-900/40 backdrop-blur-sm lg:hidden"
          onClick={onClose}
        />
      )}

      <aside
        className={`of-safe-y fixed inset-y-0 left-0 z-40 flex w-64 flex-col border-r border-slate-200 bg-white transition-transform duration-200 lg:translate-x-0 ${
          open ? "translate-x-0" : "-translate-x-full"
        }`}
      >
        {/* Logo */}
        <div className="flex items-center gap-3 px-6 py-5">
          <div className="flex h-9 w-9 items-center justify-center rounded-xl bg-gradient-to-br from-brand-400 to-brand-600 text-sm font-black text-white shadow-sm shadow-brand-600/30">
            OF
          </div>
          <div className="min-w-0 flex-1">
            <p className="text-sm font-bold text-slate-900">OfferFlow</p>
            <p className="text-[10px] font-medium text-slate-500">
              {isAdmin ? "管理控制台" : "求职智能助手"}
            </p>
          </div>
          {/* 移动端关闭按钮 */}
          <button
            onClick={onClose}
            className="flex h-8 w-8 items-center justify-center rounded-lg text-slate-400 transition hover:bg-slate-100 hover:text-slate-600 lg:hidden"
            aria-label="关闭菜单"
          >
            {Icons.x}
          </button>
        </div>

        {/* Nav */}
        <nav className="flex-1 space-y-1 overflow-y-auto px-3 py-2">
          {isAdmin ? (
            <>
              {/* 管理员只看到管理功能，不显示求职功能 */}
              <p className="px-3 py-2 text-[10px] font-bold uppercase tracking-wider text-slate-400">
                总览
              </p>
              {ADMIN_MODULES.filter((m) => !m.group).map((mod) => (
                <NavItem key={mod.key} module={mod} onClose={onClose} />
              ))}
              {adminGroups.map((g) => (
                <div key={g}>
                  <p className="px-3 pt-5 pb-2 text-[10px] font-bold uppercase tracking-wider text-slate-400">
                    {g}
                  </p>
                  {ADMIN_MODULES.filter((m) => m.group === g).map((mod) => (
                    <NavItem key={mod.key} module={mod} onClose={onClose} />
                  ))}
                </div>
              ))}

              {/* 交流中心：管理员也参与社区分享，单独放行 */}
              <p className="px-3 pt-5 pb-2 text-[10px] font-bold uppercase tracking-wider text-slate-400">
                社区
              </p>
              {MODULES.filter((m) => m.key === "community").map((mod) => (
                <NavItem key={mod.key} module={mod} onClose={onClose} />
              ))}
            </>
          ) : (
            <>
              <p className="px-3 py-2 text-[10px] font-bold uppercase tracking-wider text-slate-400">
                工作区
              </p>
              {WORKSPACE_MODULES.map((mod) => (
                <NavItem key={mod.key} module={mod} onClose={onClose} />
              ))}

              <p className="px-3 pt-5 pb-2 text-[10px] font-bold uppercase tracking-wider text-slate-400">
                系统
              </p>
              {SYSTEM_MODULES.map((mod) => (
                <NavItem key={mod.key} module={mod} onClose={onClose} />
              ))}
            </>
          )}
        </nav>

        {/* User */}
        <div className="border-t border-slate-100 px-4 py-4">
          <div className="flex items-center gap-3">
            <Avatar name={user?.name || "U"} src={user?.avatar} size={36} />
            <div className="min-w-0 flex-1">
              <p className="truncate text-sm font-medium text-slate-900">{user?.name}</p>
              <p className="text-[10px] text-slate-400">{isAdmin ? "管理员" : "成员"}</p>
            </div>
            <button
              onClick={handleLogout}
              className="flex h-8 w-8 items-center justify-center rounded-lg text-slate-400 transition hover:bg-slate-100 hover:text-rose-500 [&>svg]:h-full [&>svg]:w-full"
              title="退出登录"
            >
              {Icons.logout}
            </button>
          </div>
        </div>

        {/* 下载客户端（电脑版 / 手机版）：进入统一下载页 */}
        {!isAdmin && (
          <Link
            to="/download"
            onClick={onClose}
            className="flex items-center justify-center gap-2 border-t border-slate-100 px-4 py-3 text-xs font-medium text-slate-500 transition hover:bg-slate-50 hover:text-brand-600"
          >
            <span className="flex h-4 w-4 items-center justify-center [&>svg]:h-full [&>svg]:w-full">
              {Icons.download}
            </span>
            下载客户端（电脑 / 手机）
          </Link>
        )}
      </aside>
    </>
  );
}
