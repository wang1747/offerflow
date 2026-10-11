import { useCallback, useEffect, useMemo, useState } from "react";
import { api } from "../../api/client";
import type { AdminOverview as OverviewData } from "../../types";
import { Card, EmptyState, Loading, SectionTitle, PageHeader, Icons } from "../../components/ui";
import { formatCny, formatUsd } from "../../utils/currency";

const ALERT_STYLE: Record<string, string> = {
  error: "border-rose-200 bg-rose-50 text-rose-700",
  warning: "border-amber-200 bg-amber-50 text-amber-800",
  info: "border-sky-200 bg-sky-50 text-sky-700",
};

function Metric({ label, value, hint, tone = "slate" }: {
  label: string; value: string | number; hint?: string; tone?: string;
}) {
  const toneCls: Record<string, string> = {
    slate: "text-slate-900",
    brand: "text-brand-600",
    amber: "text-amber-600",
    rose: "text-rose-600",
    emerald: "text-emerald-600",
  };
  return (
    <div className="of-card p-4">
      <p className="text-xs font-medium text-slate-500">{label}</p>
      <p className={`mt-1 text-2xl font-bold ${toneCls[tone] || toneCls.slate}`}>{value}</p>
      {hint && <p className="mt-0.5 text-[11px] text-slate-400">{hint}</p>}
    </div>
  );
}

export default function AdminOverview() {
  const [data, setData] = useState<OverviewData | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [days, setDays] = useState(30);

  const load = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const res = await api.admin.overview(days);
      if (res.success && res.data) setData(res.data);
      else setError(res.error || "加载失败");
    } catch (err) {
      setError(err instanceof Error ? err.message : "加载失败");
    } finally {
      setLoading(false);
    }
  }, [days]);

  useEffect(() => {
    const t = window.setTimeout(() => void load(), 0);
    return () => window.clearTimeout(t);
  }, [load]);

  const maxCalls = useMemo(
    () => Math.max(...(data?.trend.map((t) => t.calls) ?? [1]), 1),
    [data],
  );

  if (loading && !data) return <Loading text="加载总览数据..." />;

  return (
    <div>
      <PageHeader title="管理总览" subtitle="系统使用情况一览" icon={Icons.dashboard} />

      {error && <div className="of-alert-error mb-4">{error}</div>}

      {/* 异常提示 */}
      {data?.alerts && data.alerts.length > 0 && (
        <div className="mb-4 space-y-2">
          {data.alerts.map((a, i) => (
            <div key={i} className={`rounded-xl border px-4 py-3 text-sm ${ALERT_STYLE[a.level] || ALERT_STYLE.info}`}>
              {a.message}
            </div>
          ))}
        </div>
      )}

      {/* 用户指标 */}
      <SectionTitle>用户</SectionTitle>
      <div className="mb-5 grid grid-cols-2 gap-3 md:grid-cols-4 lg:grid-cols-6">
        <Metric label="总用户" value={data?.users.total ?? 0} />
        <Metric label="启用中" value={data?.users.active ?? 0} tone="emerald" />
        <Metric label="7 日新增" value={data?.users.new_7d ?? 0} tone="brand" />
        <Metric label="7 日活跃" value={data?.users.active_7d ?? 0} tone="brand" hint="有 AI 调用" />
        <Metric label="自带 Key" value={data?.users.byok ?? 0} hint="自付成本" />
        <Metric label="管理员" value={data?.users.admins ?? 0} />
      </div>

      {/* 用量指标 */}
      <SectionTitle>AI 用量与成本</SectionTitle>
      <div className="mb-5 grid grid-cols-2 gap-3 md:grid-cols-3 lg:grid-cols-6">
        <Metric label="总调用" value={data?.usage.calls ?? 0} />
        <Metric label="总 Token" value={(data?.usage.tokens ?? 0).toLocaleString()} />
        <Metric
          label="总成本"
          value={formatCny(data?.usage.cost_cny)}
          hint={`≈ ${formatUsd(data?.usage.cost)}（按参考汇率折算）`}
          tone="amber"
        />
        <Metric label="失败调用" value={data?.usage.failed ?? 0} tone={(data?.usage.failed ?? 0) > 0 ? "rose" : "slate"} />
        <Metric
          label="失败率"
          value={`${data?.usage.fail_rate ?? 0}%`}
          tone={(data?.usage.fail_rate ?? 0) >= 20 ? "rose" : "slate"}
        />
        <Metric label="平均耗时" value={`${data?.usage.avg_duration_ms ?? 0}ms`} />
      </div>

      {/* 趋势 */}
      <Card className="mb-5 p-5">
        <div className="mb-4 flex items-center justify-between">
          <h3 className="text-sm font-semibold text-slate-800">调用趋势</h3>
          <div className="flex gap-1 rounded-lg bg-slate-100 p-0.5">
            {[7, 30, 90].map((d) => (
              <button
                key={d}
                onClick={() => setDays(d)}
                className={`rounded-md px-2.5 py-1 text-xs font-medium transition ${
                  days === d ? "bg-white text-brand-600 shadow-sm" : "text-slate-500 hover:text-slate-700"
                }`}
              >
                {d} 天
              </button>
            ))}
          </div>
        </div>

        {data && data.usage.calls > 0 ? (
          <>
            <div className="flex h-36 items-end gap-[3px]">
              {data.trend.map((t) => {
                const h = Math.max((t.calls / maxCalls) * 100, t.calls > 0 ? 5 : 0);
                return (
                  <div
                    key={t.date}
                    className="group relative flex-1 rounded-t bg-brand-400 transition hover:bg-brand-600"
                    style={{ height: `${h}%` }}
                    title={`${t.date}\n调用 ${t.calls} 次\n成本 ${formatCny(t.cost_cny)}`}
                  />
                );
              })}
            </div>
            <div className="mt-2 flex justify-between text-[11px] text-slate-400">
              <span>{data.trend[0]?.date}</span>
              <span>{data.trend[data.trend.length - 1]?.date}</span>
            </div>
          </>
        ) : (
          <EmptyState
            title="暂无调用数据"
            description="有新调用后这里会显示趋势（若确有使用却无数据，请查看「系统健康」）"
          />
        )}
      </Card>

      {/* 榜单 */}
      <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
        <Card className="p-5">
          <h3 className="mb-3 text-sm font-semibold text-slate-800">用量最高的用户</h3>
          {data && data.top_users.length > 0 ? (
            <div className="space-y-2.5">
              {data.top_users.map((u) => (
                <div key={u.user_id} className="flex items-center justify-between gap-3 text-sm">
                  <div className="min-w-0">
                    <span className="font-medium text-slate-800">{u.name}</span>
                    {u.byok && (
                      <span className="ml-2 rounded bg-emerald-50 px-1.5 py-0.5 text-[10px] font-medium text-emerald-600">
                        自带Key
                      </span>
                    )}
                    <span className="block truncate text-xs text-slate-400">{u.email || u.user_id}</span>
                  </div>
                  <div className="flex-shrink-0 text-right">
                    <span className="block text-sm font-medium text-slate-700">{u.calls} 次</span>
                    <span className="block text-xs text-slate-400">{formatCny(u.cost_cny)}</span>
                  </div>
                </div>
              ))}
            </div>
          ) : (
            <p className="py-6 text-center text-sm text-slate-400">暂无数据</p>
          )}
        </Card>

        <Card className="p-5">
          <h3 className="mb-3 text-sm font-semibold text-slate-800">调用最多的功能</h3>
          {data && data.top_operations.length > 0 ? (
            <div className="space-y-2.5">
              {data.top_operations.map((o) => (
                <div key={o.operation} className="flex items-center justify-between gap-3 text-sm">
                  <span className="font-mono text-xs text-slate-600">{o.operation}</span>
                  <div className="flex-shrink-0 text-right">
                    <span className="block text-sm font-medium text-slate-700">{o.calls} 次</span>
                    <span className="block text-xs text-slate-400">
                      {o.avg_duration_ms}ms · {formatCny(o.cost_cny)}
                    </span>
                  </div>
                </div>
              ))}
            </div>
          ) : (
            <p className="py-6 text-center text-sm text-slate-400">暂无数据</p>
          )}
        </Card>
      </div>

      {/* 内容总览 */}
      <Card className="mt-4 p-5">
        <h3 className="mb-3 text-sm font-semibold text-slate-800">平台内容量</h3>
        <div className="grid grid-cols-2 gap-3 text-sm sm:grid-cols-4">
          {data &&
            Object.entries(data.content).map(([k, v]) => (
              <div key={k} className="rounded-lg bg-slate-50 px-3 py-2">
                <p className="text-xs text-slate-500">{CONTENT_LABELS[k] || k}</p>
                <p className="mt-0.5 text-lg font-semibold text-slate-800">{v}</p>
              </div>
            ))}
        </div>
      </Card>
    </div>
  );
}

const CONTENT_LABELS: Record<string, string> = {
  resumes: "简历",
  jds: "岗位描述",
  applications: "投递记录",
  interview_sessions: "模拟面试",
  corpus: "共享语料",
  posts: "社区帖子",
  comments: "社区评论",
  feedbacks: "用户反馈",
};
