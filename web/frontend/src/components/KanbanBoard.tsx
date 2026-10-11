import { type FC, useState } from "react";
import type { Application, ApplicationStatus } from "../types";
import { STATUS_LABELS, STATUS_ORDER } from "../constants/application";

interface KanbanBoardProps {
  items: Application[];
  onStatusChange: (app: Application, status: ApplicationStatus) => void;
  onEdit: (app: Application) => void;
  onDelete: (app: Application) => void;
}

const COLUMN_COLORS: Record<string, string> = {
  saved: "bg-slate-100 text-slate-600",
  applied: "bg-blue-50 text-blue-600",
  online_test: "bg-violet-50 text-violet-600",
  first_interview: "bg-cyan-50 text-cyan-600",
  second_interview: "bg-teal-50 text-teal-600",
  hr_round: "bg-amber-50 text-amber-600",
  offered: "bg-emerald-50 text-emerald-600",
  accepted: "bg-green-50 text-green-600",
  rejected: "bg-rose-50 text-rose-600",
};

const KanbanBoard: FC<KanbanBoardProps> = ({ items, onStatusChange, onEdit, onDelete }) => {
  const [dragApp, setDragApp] = useState<Application | null>(null);
  const [dragOver, setDragOver] = useState<string | null>(null);

  const grouped = STATUS_ORDER.reduce((acc, status) => {
    acc[status] = items.filter((item) => item.status === status);
    return acc;
  }, {} as Record<string, Application[]>);

  const handleDrop = (status: string) => {
    if (dragApp && dragApp.status !== status) {
      onStatusChange(dragApp, status as ApplicationStatus);
    }
    setDragApp(null);
    setDragOver(null);
  };

  return (
    <div className="flex gap-3 overflow-x-auto pb-2 min-h-[400px]">
      {STATUS_ORDER.map((status) => {
        const cards = grouped[status] || [];
        const isDragOver = dragOver === status;
        return (
          <div
            key={status}
            className={`min-w-[200px] flex-1 rounded-xl transition-colors ${
              isDragOver ? "bg-brand-50 ring-2 ring-brand-200" : ""
            }`}
            onDragOver={(e) => {
              e.preventDefault();
              if (dragOver !== status) setDragOver(status);
            }}
            onDragLeave={(e) => {
              // 只在真正离开列容器时清除高亮（避免子元素间移动误触发）
              if (!e.currentTarget.contains(e.relatedTarget as Node)) {
                setDragOver((cur) => (cur === status ? null : cur));
              }
            }}
            onDrop={(e) => {
              e.preventDefault();
              handleDrop(status);
            }}
          >
            <div className={`mb-2 flex items-center justify-between rounded-lg px-3 py-2 text-xs font-semibold ${COLUMN_COLORS[status] || "bg-slate-100 text-slate-600"}`}>
              <span>{STATUS_LABELS[status]}</span>
              <span className="rounded-full bg-white/60 px-2">{cards.length}</span>
            </div>
            <div className="flex flex-col gap-2">
              {cards.map((app) => (
                <div
                  key={app.id}
                  draggable
                  onDragStart={() => setDragApp(app)}
                  onDragEnd={() => {
                    setDragApp(null);
                    setDragOver(null);
                  }}
                  className={`of-card-hover p-3 ${dragApp?.id === app.id ? "opacity-50" : ""} cursor-grab active:cursor-grabbing`}
                >
                  <div className="flex items-start justify-between gap-2">
                    <div className="min-w-0">
                      <div className="truncate text-sm font-medium text-slate-900">{app.company}</div>
                      <div className="truncate text-xs text-slate-500">{app.position}</div>
                    </div>
                    <button
                      onClick={() => onDelete(app)}
                      className="flex-shrink-0 text-xs text-slate-300 transition hover:text-rose-500"
                      aria-label={`删除 ${app.company}`}
                    >
                      <svg width="14" height="14" viewBox="0 0 16 16" fill="none">
                        <path d="M3 4h10M6.5 4V3a1 1 0 0 1 1-1h1a1 1 0 0 1 1 1v1M5 4l.5 9a1 1 0 0 0 1 1h3a1 1 0 0 0 1-1l.5-9" stroke="currentColor" strokeWidth="1.2" strokeLinecap="round" />
                      </svg>
                    </button>
                  </div>
                  {app.next_action && (
                    <div className="mt-2 truncate rounded bg-slate-50 px-2 py-1 text-xs text-slate-600">{app.next_action}</div>
                  )}
                  {app.next_action_date && (
                    <div className="mt-1 text-xs text-slate-400">{app.next_action_date}</div>
                  )}
                  <div className="mt-3 space-y-1.5">
                    <select
                      value={app.status}
                      onChange={(e) => onStatusChange(app, e.target.value as ApplicationStatus)}
                      className="w-full rounded-lg border border-slate-200 bg-white px-2 py-1 text-xs text-slate-600 outline-none focus:border-brand-400"
                      aria-label={`${app.company} 状态`}
                    >
                      {STATUS_ORDER.map((s) => (
                        <option key={s} value={s}>{STATUS_LABELS[s]}</option>
                      ))}
                    </select>
                    <button
                      onClick={() => onEdit(app)}
                      className="w-full rounded-lg border border-slate-200 px-2 py-1 text-xs text-slate-500 transition hover:border-brand-300 hover:text-brand-600"
                    >
                      编辑详情
                    </button>
                  </div>
                </div>
              ))}
              {cards.length === 0 && (
                <div className="rounded-lg border border-dashed border-slate-200 py-4 text-center text-xs text-slate-300">
                  空
                </div>
              )}
            </div>
          </div>
        );
      })}
    </div>
  );
};

export default KanbanBoard;
