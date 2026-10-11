import { type ReactNode, useEffect, useRef, useState } from "react";

// ===================== TableScroll =====================
/**
 * 宽表格在窄屏上的可滚动容器。
 *
 * 手机宽度（约 375px）下，620~900px 的表格只能露出 40% 左右，
 * 用户往往误以为「表格就这几列」而错过右侧的操作按钮。
 * 这里做两件事：
 *   1. 右侧有未显示内容时，盖一层渐隐遮罩；
 *   2. 窄屏下在表格底部提示「左右滑动」（桌面端不显示）。
 */
export function TableScroll({
  children,
  hint = "左右滑动查看更多",
}: {
  children: ReactNode;
  hint?: string;
}) {
  const ref = useRef<HTMLDivElement>(null);
  const [overflowing, setOverflowing] = useState(false);
  const [atEnd, setAtEnd] = useState(false);

  const measure = () => {
    const el = ref.current;
    if (!el) return;
    const more = el.scrollWidth - el.clientWidth > 4;
    setOverflowing(more);
    setAtEnd(el.scrollLeft + el.clientWidth >= el.scrollWidth - 4);
  };

  useEffect(() => {
    measure();
    const el = ref.current;
    if (!el) return;
    // 内容/字体加载完成后宽度可能变化，用 ResizeObserver 兜住
    const ro = new ResizeObserver(measure);
    ro.observe(el);
    window.addEventListener("resize", measure);
    return () => {
      ro.disconnect();
      window.removeEventListener("resize", measure);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  return (
    <div>
      {/* 提示放在表格上方：放下方要滚动到底才看得到，起不到提示作用 */}
      {overflowing && (
        <p className="mb-2 text-center text-[11px] text-slate-400 sm:hidden">
          ← {hint} →
        </p>
      )}
      <div className="relative">
        <div
          ref={ref}
          onScroll={measure}
          className="of-table-scroll touch-pan-x overflow-x-auto overscroll-x-contain"
        >
          {children}
        </div>

        {overflowing && !atEnd && (
          <div className="pointer-events-none absolute inset-y-0 right-0 w-10 bg-gradient-to-l from-white to-transparent" />
        )}
      </div>
    </div>
  );
}

// ===================== Card =====================
export function Card({
  children,
  className = "",
  hover = false,
}: {
  children: ReactNode;
  className?: string;
  hover?: boolean;
}) {
  return (
    <div className={`${hover ? "of-card-hover" : "of-card"} ${className}`}>
      {children}
    </div>
  );
}

// ===================== PageHeader =====================
export function PageHeader({
  title,
  subtitle,
  icon,
  actions,
}: {
  title: string;
  subtitle?: string;
  icon?: ReactNode;
  actions?: ReactNode;
}) {
  return (
    <div className="mb-6 flex flex-wrap items-center justify-between gap-3">
      <div className="flex items-center gap-3">
        {icon && (
          <div className="flex h-10 w-10 items-center justify-center rounded-xl bg-brand-50 text-brand-600 [&>svg]:h-full [&>svg]:w-full">
            {icon}
          </div>
        )}
        <div>
          <h1 className="of-page-title">{title}</h1>
          {subtitle && <p className="mt-0.5 text-sm text-slate-500">{subtitle}</p>}
        </div>
      </div>
      {actions && <div className="flex flex-wrap items-center gap-2">{actions}</div>}
    </div>
  );
}

// ===================== SectionTitle =====================
export function SectionTitle({
  children,
  count,
  right,
}: {
  children: ReactNode;
  count?: number;
  right?: ReactNode;
}) {
  return (
    <div className="mb-4 flex items-center justify-between">
      <h2 className="of-section-title flex items-center gap-2">
        {children}
        {count !== undefined && (
          <span className="of-badge bg-slate-100 text-slate-500">{count}</span>
        )}
      </h2>
      {right}
    </div>
  );
}

// ===================== Badge =====================
const badgeColors = {
  gray: "bg-slate-100 text-slate-600",
  blue: "bg-blue-50 text-blue-600",
  green: "bg-emerald-50 text-emerald-600",
  red: "bg-rose-50 text-rose-600",
  amber: "bg-amber-50 text-amber-600",
  indigo: "bg-brand-50 text-brand-600",
  cyan: "bg-cyan-50 text-cyan-600",
} as const;

export function Badge({
  children,
  color = "gray",
}: {
  children: ReactNode;
  color?: keyof typeof badgeColors;
}) {
  return (
    <span className={`of-badge ${badgeColors[color]}`}>{children}</span>
  );
}

// ===================== EmptyState =====================
export function EmptyState({
  icon,
  title,
  description,
}: {
  icon?: ReactNode;
  title: string;
  description?: string;
}) {
  return (
    <div className="of-empty">
      {icon && <div className="mb-3 text-slate-300 [&>svg]:h-8 [&>svg]:w-8">{icon}</div>}
      <p className="text-sm font-medium text-slate-500">{title}</p>
      {description && <p className="mt-1 text-xs text-slate-400">{description}</p>}
    </div>
  );
}

// ===================== StatCard =====================
export function StatCard({
  label,
  value,
  sub,
  color = "slate",
  icon,
}: {
  label: string;
  value: ReactNode;
  sub?: string;
  color?: "slate" | "blue" | "green" | "amber" | "indigo" | "rose";
  icon?: ReactNode;
}) {
  const colorMap = {
    slate: "text-slate-900",
    blue: "text-blue-600",
    green: "text-emerald-600",
    amber: "text-amber-600",
    indigo: "text-brand-600",
    rose: "text-rose-600",
  };
  return (
    <div className="of-card-hover p-4">
      <div className="flex items-start justify-between">
        <div>
          <p className="text-xs font-medium text-slate-500">{label}</p>
          <p className={`mt-1 text-2xl font-bold ${colorMap[color]}`}>{value}</p>
          {sub && <p className="mt-0.5 text-xs text-slate-400">{sub}</p>}
        </div>
        {icon && (
          <div className="flex h-9 w-9 items-center justify-center rounded-lg bg-slate-50 text-slate-400 [&>svg]:h-full [&>svg]:w-full">
            {icon}
          </div>
        )}
      </div>
    </div>
  );
}

// ===================== Modal =====================
export function Modal({
  open,
  onClose,
  title,
  children,
  footer,
}: {
  open: boolean;
  onClose: () => void;
  title: string;
  children: ReactNode;
  footer?: ReactNode;
}) {
  if (!open) return null;
  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-slate-900/40 p-4 backdrop-blur-sm animate-fade-in"
      onClick={onClose}
    >
      <div
        className="max-h-[90vh] w-full max-w-lg overflow-y-auto rounded-2xl bg-white p-6 shadow-2xl animate-slide-up"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="mb-4 flex items-center justify-between">
          <h2 className="text-lg font-bold text-slate-900">{title}</h2>
          <button
            onClick={onClose}
            className="flex h-8 w-8 items-center justify-center rounded-lg text-slate-400 transition hover:bg-slate-100 hover:text-slate-600"
            aria-label="close"
          >
            <svg width="16" height="16" viewBox="0 0 16 16" fill="none">
              <path d="M3.5 3.5l9 9M12.5 3.5l-9 9" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
            </svg>
          </button>
        </div>
        {children}
        {footer && <div className="mt-5 flex justify-end gap-3">{footer}</div>}
      </div>
    </div>
  );
}

// ===================== Tabs =====================
export function Tabs({
  tabs,
  active,
  onChange,
}: {
  tabs: { key: string; label: string; icon?: ReactNode }[];
  active: string;
  onChange: (key: string) => void;
}) {
  return (
    <div className="mb-6 flex gap-1 overflow-x-auto rounded-xl bg-slate-100 p-1">
      {tabs.map((tab) => (
        <button
          key={tab.key}
          onClick={() => onChange(tab.key)}
          className={`flex flex-shrink-0 items-center gap-2 rounded-lg px-4 py-2 text-sm font-medium transition-all duration-200 ${
            active === tab.key
              ? "bg-white text-brand-600 shadow-soft"
              : "text-slate-500 hover:text-slate-700"
          }`}
        >
          {tab.icon}
          {tab.label}
        </button>
      ))}
    </div>
  );
}

// ===================== Loading =====================
export function Loading({ text = "loading..." }: { text?: string }) {
  return (
    <div className="flex items-center justify-center py-12">
      <div className="flex items-center gap-3 text-slate-400">
        <div className="h-5 w-5 animate-spin rounded-full border-2 border-slate-200 border-t-brand-500" />
        <span className="text-sm">{text}</span>
      </div>
    </div>
  );
}

// ===================== Icons (inline SVG, no dependency) =====================
const icon = (children: ReactNode) => (
  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" className="h-[1em] w-[1em] shrink-0">
    {children}
  </svg>
);

export const Icons = {
  dashboard: icon(<><rect x="3" y="3" width="7" height="9" rx="1" /><rect x="14" y="3" width="7" height="5" rx="1" /><rect x="14" y="12" width="7" height="9" rx="1" /><rect x="3" y="16" width="7" height="5" rx="1" /></>),
  track: icon(<><path d="M3 3v18h18" /><path d="M7 14l3-3 3 3 5-5" /></>),
  resume: icon(<><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" /><path d="M14 2v6h6" /><path d="M9 13h6M9 17h6" /></>),
  match: icon(<><path d="M16 3h5v5" /><path d="M8 3H3v5" /><path d="M3 8c5 4 13 4 18 0" /><path d="M8 21H3v-5" /><path d="M16 21h5v-5" /></>),
  interview: icon(<><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z" /></>),
  settings: icon(<><circle cx="12" cy="12" r="3" /><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1 0 2.83 2 2 0 0 1-2.83 0l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83 0 2 2 0 0 1 0-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 0-2.83 2 2 0 0 1 2.83 0l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 0 2 2 0 0 1 0 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z" /></>),
  admin: icon(<><path d="M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2" /><circle cx="9" cy="7" r="4" /><path d="M23 21v-2a4 4 0 0 0-3-3.87" /><path d="M16 3.13a4 4 0 0 1 0 7.75" /></>),
  upload: icon(<><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4" /><path d="M17 8l-5-5-5 5" /><path d="M12 3v12" /></>),
  download: icon(<><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4" /><path d="M7 10l5 5 5-5" /><path d="M12 15V3" /></>),
  sparkle: icon(<><path d="M12 3l1.5 5L19 9.5 13.5 11 12 16l-1.5-5L5 9.5 10.5 8z" /></>),
  chart: icon(<><path d="M3 3v18h18" /><rect x="7" y="10" width="3" height="8" /><rect x="14" y="6" width="3" height="12" /></>),
  check: icon(<><path d="M20 6L9 17l-5-5" /></>),
  x: icon(<><path d="M18 6L6 18M6 6l12 12" /></>),
  alert: icon(<><circle cx="12" cy="12" r="10" /><path d="M12 8v4M12 16h.01" /></>),
  rocket: icon(<><path d="M4.5 16.5c-1.5 1.26-2 5-2 5s3.74-.5 5-2c.71-.84.7-2.13-.09-2.91a2.18 2.18 0 0 0-3.16-.1z" /><path d="M12 15l-3-3a22 22 0 0 1 2-3.95A12.88 12.88 0 0 1 22 2c0 2.72-.78 7.5-6 11a22.35 22.35 0 0 1-4 2z" /><path d="M9 12h.01M15 18h.01" /></>),
  logout: icon(<><path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4" /><path d="M16 17l5-5-5-5" /><path d="M21 12H9" /></>),
  menu: icon(<><path d="M3 6h18M3 12h18M3 18h18" /></>),
  money: icon(<><circle cx="12" cy="12" r="9" /><path d="M12 7v10" /><path d="M15 9.5c0-1.1-.9-2-2-2h-1.5c-.8 0-1.5.7-1.5 1.5s.7 1.5 1.5 1.5h1c.8 0 1.5.7 1.5 1.5s-.7 1.5-1.5 1.5H11c-1.1 0-2-.9-2-2" /></>),
};
