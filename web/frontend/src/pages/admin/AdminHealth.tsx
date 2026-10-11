import { useCallback, useEffect, useState } from "react";
import { api } from "../../api/client";
import type { AdminHealth as HealthData, AdminTraceRow } from "../../types";
import { Card, Icons, Loading, PageHeader, SectionTitle, TableScroll } from "../../components/ui";

function TraceTable({ rows, emptyText }: { rows: AdminTraceRow[]; emptyText: string }) {
  if (rows.length === 0) {
    return <p className="py-6 text-center text-sm text-slate-400">{emptyText}</p>;
  }
  return (
    <TableScroll>
      <table className="w-full min-w-[620px] text-left text-sm">
        <thead>
          <tr className="border-b border-slate-100 text-slate-500">
            <th className="py-2 pr-4 font-medium">时间</th>
            <th className="py-2 pr-4 font-medium">用户</th>
            <th className="py-2 pr-4 font-medium">功能</th>
            <th className="py-2 pr-4 font-medium">状态</th>
            <th className="py-2 text-right font-medium">耗时</th>
          </tr>
        </thead>
        <tbody className="divide-y divide-slate-50">
          {rows.map((r) => (
            <tr key={r.id}>
              <td className="whitespace-nowrap py-2.5 pr-4 text-xs text-slate-500">
                {r.created_at ? new Date(`${r.created_at}Z`).toLocaleString("zh-CN") : "-"}
              </td>
              <td className="py-2.5 pr-4 text-slate-700">{r.user_name || r.user_id || "-"}</td>
              <td className="py-2.5 pr-4">
                <span className="font-mono text-xs text-slate-600">{r.operation}</span>
              </td>
              <td className="py-2.5 pr-4">
                <span
                  className={`of-badge ${
                    r.status === "success"
                      ? "bg-emerald-50 text-emerald-600"
                      : "bg-rose-50 text-rose-600"
                  }`}
                >
                  {r.status === "success" ? "成功" : r.status}
                </span>
              </td>
              <td className="py-2.5 text-right text-slate-500">{r.duration_ms}ms</td>
            </tr>
          ))}
        </tbody>
      </table>
    </TableScroll>
  );
}

export default function AdminHealth() {
  const [data, setData] = useState<HealthData | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");

  const load = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const res = await api.admin.health();
      if (res.success && res.data) setData(res.data);
      else setError(res.error || "加载失败");
    } catch (err) {
      setError(err instanceof Error ? err.message : "加载失败");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    const t = window.setTimeout(() => void load(), 0);
    return () => window.clearTimeout(t);
  }, [load]);

  if (loading && !data) return <Loading text="检查系统状态..." />;

  const statusMeta: Record<string, { label: string; cls: string }> = {
    ok: { label: "运行正常", cls: "bg-emerald-50 text-emerald-700 border-emerald-200" },
    degraded: { label: "降级运行", cls: "bg-amber-50 text-amber-800 border-amber-200" },
    down: { label: "异常", cls: "bg-rose-50 text-rose-700 border-rose-200" },
  };
  const meta = statusMeta[data?.status || "down"] || statusMeta.down;

  return (
    <div>
      <PageHeader title="系统健康" subtitle="服务状态与失败调用" icon={Icons.alert} />

      {error && <div className="of-alert-error mb-4">{error}</div>}

      <div className={`mb-5 flex items-center justify-between rounded-xl border px-5 py-4 ${meta.cls}`}>
        <div>
          <p className="text-sm font-semibold">系统状态：{meta.label}</p>
          <p className="mt-0.5 text-xs opacity-80">
            检查时间 {data?.checked_at ? new Date(`${data.checked_at}Z`).toLocaleString("zh-CN") : "-"}
          </p>
        </div>
        <button onClick={load} className="of-btn-ghost text-xs" disabled={loading}>
          {loading ? "检查中..." : "重新检查"}
        </button>
      </div>

      <div className="mb-5 grid grid-cols-2 gap-3 md:grid-cols-4">
        <div className="of-card p-4">
          <p className="text-xs text-slate-500">数据库</p>
          <p className={`mt-1 text-lg font-bold ${data?.database.ok ? "text-emerald-600" : "text-rose-600"}`}>
            {data?.database.ok ? "连接正常" : "不可用"}
          </p>
        </div>
        <div className="of-card p-4">
          <p className="text-xs text-slate-500">平台默认模型</p>
          <p className="mt-1 text-lg font-bold text-slate-900">{data?.default_key.model || "-"}</p>
          <p className="mt-0.5 text-[11px] text-slate-400">
            {data?.default_key.configured ? "Key 已配置" : "Key 未配置"}
          </p>
        </div>
        <div className="of-card p-4">
          <p className="text-xs text-slate-500">近 1 小时调用</p>
          <p className="mt-1 text-lg font-bold text-slate-900">{data?.traces.last_1h ?? 0}</p>
        </div>
        <div className="of-card p-4">
          <p className="text-xs text-slate-500">近 24 小时调用</p>
          <p className="mt-1 text-lg font-bold text-slate-900">{data?.traces.last_24h ?? 0}</p>
        </div>
      </div>

      {data && data.traces.total === 0 && (
        <div className="mb-5 rounded-xl border border-amber-200 bg-amber-50 px-4 py-3 text-sm text-amber-800">
          尚无任何调用记录。如果确实有人在用 AI 功能，说明用量采集异常，需要检查 observability 落库。
        </div>
      )}

      <Card className="mb-5 p-5">
        <SectionTitle count={data?.recent_failures.length}>最近失败的调用</SectionTitle>
        <TraceTable rows={data?.recent_failures ?? []} emptyText="没有失败记录，很好" />
      </Card>

      <Card className="p-5">
        <SectionTitle count={data?.slowest.length}>最慢的调用</SectionTitle>
        <TraceTable rows={data?.slowest ?? []} emptyText="暂无调用记录" />
      </Card>
    </div>
  );
}
