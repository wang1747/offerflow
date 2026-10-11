import { type FC, useCallback, useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../api/client";
import { STATUS_LABELS } from "../constants/application";
import type { Application, ApplicationStats, MatchResult, Reminders, User } from "../types";
import { Card, PageHeader, StatCard, Badge, EmptyState, Icons } from "../components/ui";

const formatDate = (iso?: string) => {
  if (!iso) return "-";
  const d = new Date(iso);
  return `${d.toLocaleDateString()} ${d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}`;
};

const STATUS_BADGE: Record<string, "gray" | "blue" | "green" | "amber" | "red" | "indigo" | "cyan"> = {
  saved: "gray", applied: "blue", online_test: "indigo", first_interview: "cyan",
  second_interview: "green", hr_round: "amber", offered: "green", accepted: "green", rejected: "red",
};

const Dashboard: FC = () => {
  const [user, setUser] = useState<User | null>(null);
  const [health, setHealth] = useState("检查中...");
  const [apps, setApps] = useState<Application[]>([]);
  const [stats, setStats] = useState<ApplicationStats | null>(null);
  const [reminders, setReminders] = useState<Reminders | null>(null);
  const [rankings, setRankings] = useState<MatchResult[]>([]);
  const [showModelBanner, setShowModelBanner] = useState(false);

  const loadAll = useCallback(async () => {
    try {
      const [userRes, appsRes, statsRes, remindRes, rankRes] = await Promise.all([
        api.auth.me(), api.applications.list(), api.applications.stats(),
        api.applications.reminders(), api.match.rankings(),
      ]);
      if (userRes.success && userRes.data) setUser(userRes.data);
      if (appsRes.success && appsRes.data) setApps(appsRes.data);
      if (statsRes.success && statsRes.data) setStats(statsRes.data);
      if (remindRes.success && remindRes.data) setReminders(remindRes.data);
      if (rankRes.success && rankRes.data) setRankings(rankRes.data);
    } catch (err) {
      console.error("加载总览数据失败:", err);
    }
  }, []);

  const checkModelConfig = useCallback(async () => {
    try {
      const res = await api.modelConfig.get();
      // 有平台默认模型兜底时（using_default）功能可用，不再提示「尚未配置」
      if (res.success && res.data) {
        setShowModelBanner(!res.data.has_config && !res.data.using_default);
      }
    } catch (err) {
      console.error("检查模型配置失败:", err);
    }
  }, []);

  useEffect(() => {
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const checkHealth = async () => {
      try {
        const res = await api.health();
        if (cancelled) return;
        setHealth(res.data?.status || "未连接");
        if (res.data?.status === "ok") {
          await loadAll();
          await checkModelConfig();
        } else {
          timer = setTimeout(checkHealth, 3000);
        }
      } catch {
        if (cancelled) return;
        setHealth("连接失败");
        timer = setTimeout(checkHealth, 3000);
      }
    };
    checkHealth();
    return () => { cancelled = true; if (timer) clearTimeout(timer); };
  }, [loadAll, checkModelConfig]);

  const offerCount = (stats?.status_counts.offered || 0) + (stats?.status_counts.accepted || 0);
  const reminderCount = (reminders?.overdue_count || 0) + (reminders?.upcoming_count || 0);

  return (
    <div>
      <PageHeader
        title={`欢迎回来${user ? `，${user.name}` : ""}`}
        subtitle="这是你的求职仪表盘，快速了解整体进展"
        actions={
          <div className={`of-badge ${health === "ok" ? "bg-emerald-50 text-emerald-600" : "bg-rose-50 text-rose-600"}`}>
            <span className={`h-2 w-2 rounded-full ${health === "ok" ? "bg-emerald-500" : "bg-rose-500"}`} />
            {health === "ok" ? "运行中" : health}
          </div>
        }
      />

      {showModelBanner && (
        <div className="mb-6 flex flex-wrap items-center justify-between gap-3 rounded-xl border border-amber-200 bg-amber-50 px-5 py-3.5 text-sm text-amber-800">
          <div className="flex items-center gap-2">
            {Icons.alert}
            <span>尚未配置大模型，部分 AI 功能暂不可用。</span>
          </div>
          <Link to="/preferences?tab=model" className="of-btn bg-amber-600 px-3 py-1.5 text-white hover:bg-amber-700">
            去配置
          </Link>
        </div>
      )}

      {apps.length === 0 && (
        <Card className="mb-6 border-brand-200 bg-gradient-to-br from-brand-50/60 to-white p-5">
          <div className="mb-4">
            <h2 className="text-base font-semibold text-slate-900">第一次求职？跟着这 3 步开始</h2>
            <p className="mt-0.5 text-sm text-slate-500">几分钟就能做出第一份针对心仪岗位的简历</p>
          </div>
          <div className="grid grid-cols-1 gap-3 sm:grid-cols-3">
            <Link to="/resume/generate" className="group rounded-xl border border-slate-200 bg-white p-4 transition hover:border-brand-300 hover:shadow-sm">
              <div className="flex items-center gap-2">
                <span className="flex h-6 w-6 items-center justify-center rounded-full bg-brand-600 text-xs font-bold text-white">1</span>
                <span className="text-sm font-medium text-slate-900">做一份简历</span>
              </div>
              <p className="mt-2 text-xs leading-relaxed text-slate-500">没有简历？告诉 AI 你的学校和经历，自动生成一份</p>
            </Link>
            <Link to="/job" className="group rounded-xl border border-slate-200 bg-white p-4 transition hover:border-brand-300 hover:shadow-sm">
              <div className="flex items-center gap-2">
                <span className="flex h-6 w-6 items-center justify-center rounded-full bg-brand-600 text-xs font-bold text-white">2</span>
                <span className="text-sm font-medium text-slate-900">分析职位要求</span>
              </div>
              <p className="mt-2 text-xs leading-relaxed text-slate-500">粘贴心仪岗位的招聘要求，AI 帮你拆出重点</p>
            </Link>
            <Link to="/job" className="group rounded-xl border border-slate-200 bg-white p-4 transition hover:border-brand-300 hover:shadow-sm">
              <div className="flex items-center gap-2">
                <span className="flex h-6 w-6 items-center justify-center rounded-full bg-brand-600 text-xs font-bold text-white">3</span>
                <span className="text-sm font-medium text-slate-900">看你和岗位的匹配</span>
              </div>
              <p className="mt-2 text-xs leading-relaxed text-slate-500">看看简历和岗位差在哪，照着补</p>
            </Link>
          </div>
        </Card>
      )}

      <div className="mb-6 grid grid-cols-2 gap-3 lg:grid-cols-4">
        <StatCard label="投递总数" value={stats?.total ?? 0} color="indigo" icon={Icons.track} />
        <StatCard label="Offer 数" value={offerCount} color="green" icon={Icons.rocket} />
        <StatCard label="投递→面试" value={stats ? `${stats.conversion_rate.applied_to_interview}%` : "-"} color="amber" />
        <StatCard label="待跟进" value={reminderCount} color={reminderCount > 0 ? "rose" : "slate"} />
      </div>

      <div className="grid grid-cols-1 gap-5 lg:grid-cols-2">
        <Card className="p-5">
          <div className="mb-4 flex items-center justify-between">
            <h2 className="of-section-title">最近投递</h2>
            <Link to="/applications" className="text-xs font-medium text-brand-600 hover:text-brand-700">查看全部</Link>
          </div>
          {apps.length === 0 ? (
            <EmptyState title="暂无投递记录" description="去投递追踪添加你的第一条记录" />
          ) : (
            <div className="space-y-2">
              {apps.slice(0, 6).map((app) => (
                <div key={app.id} className="flex items-center justify-between gap-3 rounded-xl bg-slate-50 px-4 py-2.5">
                  <div className="min-w-0">
                    <div className="truncate text-sm font-medium text-slate-900">{app.company} · {app.position}</div>
                    <div className="mt-0.5 text-xs text-slate-400">{formatDate(app.updated_at)}</div>
                  </div>
                  <Badge color={STATUS_BADGE[app.status] || "gray"}>{STATUS_LABELS[app.status] || app.status}</Badge>
                </div>
              ))}
            </div>
          )}
        </Card>

        <Card className="p-5">
          <h2 className="of-section-title mb-4">提醒</h2>
          {reminders && reminders.has_reminders ? (
            <div className="space-y-4">
              <div>
                <h3 className="mb-2 flex items-center gap-2 text-sm font-medium text-rose-600">
                  <span className="h-2 w-2 rounded-full bg-rose-500" />
                  超期未跟进 ({reminders.overdue_count})
                </h3>
                <ul className="space-y-1.5">
                  {reminders.overdue.slice(0, 4).map((item) => (
                    <li key={item.id} className="flex items-center justify-between gap-3 rounded-lg px-3 py-1.5 text-sm">
                      <span className="min-w-0 truncate text-slate-700">{item.company} · {item.position}</span>
                      <span className="flex-shrink-0 font-medium text-rose-500">{item.days_since_update} 天</span>
                    </li>
                  ))}
                </ul>
              </div>
              <div>
                <h3 className="mb-2 flex items-center gap-2 text-sm font-medium text-blue-600">
                  <span className="h-2 w-2 rounded-full bg-blue-500" />
                  即将到来 ({reminders.upcoming_count})
                </h3>
                <ul className="space-y-1.5">
                  {reminders.upcoming.slice(0, 4).map((item) => (
                    <li key={item.id} className="flex items-center justify-between gap-3 rounded-lg px-3 py-1.5 text-sm">
                      <span className="min-w-0 truncate text-slate-700">
                        {item.company} · {item.position}
                        {item.next_action ? ` · ${item.next_action}` : ""}
                      </span>
                      <span className="flex-shrink-0 text-slate-400">{item.next_action_date}</span>
                    </li>
                  ))}
                </ul>
              </div>
            </div>
          ) : (
            <EmptyState title="暂无提醒" description="一切就绪" />
          )}
        </Card>
      </div>

      {rankings.length > 0 && (
        <Card className="mt-5 p-5">
          <div className="mb-4 flex items-center justify-between">
            <h2 className="of-section-title">匹配速览</h2>
            <Link to="/job" className="text-xs font-medium text-brand-600 hover:text-brand-700">查看全部</Link>
          </div>
          <div className="space-y-2">
            {rankings.slice(0, 4).map((item) => (
              <div key={item.id || `${item.jd_id}-${item.resume_id}`}
                className="flex items-center justify-between gap-3 rounded-xl bg-slate-50 px-4 py-2.5">
                <div className="min-w-0">
                  <div className="truncate text-sm font-medium text-slate-900">
                    {item.company || "未知公司"} · {item.position || "未知职位"}
                  </div>
                  <div className="mt-0.5 truncate text-xs text-slate-400">{item.suggestion}</div>
                </div>
                <span className={`flex-shrink-0 text-lg font-bold ${
                  item.score >= 80 ? "text-emerald-600" : item.score >= 60 ? "text-amber-600" : "text-rose-600"
                }`}>
                  {item.score}
                </span>
              </div>
            ))}
          </div>
        </Card>
      )}
    </div>
  );
};

export default Dashboard;
