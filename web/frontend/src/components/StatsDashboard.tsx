import { useCallback, useEffect, useState } from "react";
import {
  BarChart, Bar, XAxis, YAxis, CartesianGrid, Tooltip, Legend,
  ResponsiveContainer, PieChart, Pie, Cell,
} from "recharts";
import { api } from "../api/client";
import { StatCard, Icons } from "./ui";

interface StatsData {
  total: number;
  status_counts: Record<string, number>;
  status_order: string[];
  conversion_rate: { applied_to_interview: number; interview_to_offer: number };
}

const STATUS_LABELS: Record<string, string> = {
  saved: "收藏", applied: "已投递", online_test: "笔试",
  first_interview: "一面", second_interview: "二面", hr_round: "HR面",
  offered: "Offer", accepted: "已接受", rejected: "已拒绝",
};

const STATUS_COLORS: Record<string, string> = {
  saved: "#94a3b8", applied: "#3b82f6", online_test: "#8b5cf6",
  first_interview: "#06b6d4", second_interview: "#14b8a6",
  hr_round: "#f59e0b", offered: "#f97316", accepted: "#22c55e", rejected: "#ef4444",
};

/** 图表无数据时的占位：避免留下 280px 的空白块（窄屏下尤其浪费空间） */
function ChartEmpty({ text }: { text: string }) {
  return (
    <div className="flex h-[180px] flex-col items-center justify-center rounded-xl border border-dashed border-slate-200 px-4 text-center sm:h-[280px]">
      <p className="max-w-[16rem] text-xs leading-relaxed text-slate-400">{text}</p>
    </div>
  );
}

export default function StatsDashboard() {
  const [stats, setStats] = useState<StatsData | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const loadStats = useCallback(async () => {
    try {
      const resp = await api.applications.stats();
      if (resp.success && resp.data) setStats(resp.data);
      else setError(resp.error || "加载失败");
    } catch (err) {
      console.error("加载统计失败:", err);
      setError("加载统计失败");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    const timer = setTimeout(() => { void loadStats(); }, 0);
    return () => clearTimeout(timer);
  }, [loadStats]);

  if (loading) {
    return (
      <div className="flex items-center justify-center py-8">
        <div className="flex items-center gap-3 text-slate-400">
          <div className="h-5 w-5 animate-spin rounded-full border-2 border-slate-200 border-t-brand-500" />
          <span className="text-sm">加载统计中...</span>
        </div>
      </div>
    );
  }

  if (error || !stats) {
    return (
      <div className="flex items-center justify-center py-8 text-sm text-rose-500">
        {Icons.alert} <span className="ml-2">{error || "暂无数据"}</span>
      </div>
    );
  }

  const statusData = stats.status_order
    .filter((key) => stats.status_counts[key] > 0)
    .map((key) => ({ name: STATUS_LABELS[key] || key, value: stats.status_counts[key], color: STATUS_COLORS[key] || "#94a3b8" }));

  const funnelSteps = ["saved", "applied", "online_test", "first_interview", "second_interview", "hr_round", "offered"];
  const funnelData = funnelSteps
    .filter((key) => stats.status_counts[key] > 0)
    .map((key) => ({ name: STATUS_LABELS[key] || key, value: stats.status_counts[key], color: STATUS_COLORS[key] || "#94a3b8" }));

  return (
    <div className="space-y-4">
      <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
        <StatCard label="总投递" value={stats.total} color="indigo" icon={Icons.track} />
        <StatCard label="面试转化率" value={`${stats.conversion_rate.applied_to_interview}%`} sub="投递 → 一面" color="blue" />
        <StatCard label="Offer 转化率" value={`${stats.conversion_rate.interview_to_offer}%`} sub="一面 → Offer" color="green" />
        <StatCard label="已接受" value={stats.status_counts.accepted || 0} color="amber" />
      </div>

      <div className="grid gap-4 md:grid-cols-2">
        <div className="of-card p-4">
          <h3 className="mb-4 text-sm font-semibold text-slate-700">投递漏斗</h3>
          {funnelData.length === 0 ? (
            <ChartEmpty text="还没有投递记录，添加后这里会显示各阶段漏斗" />
          ) : (
          <ResponsiveContainer width="100%" height={280}>
            <BarChart data={funnelData} layout="vertical">
              <CartesianGrid strokeDasharray="3 3" stroke="#f1f5f9" />
              <XAxis type="number" tick={{ fontSize: 11, fill: "#94a3b8" }} axisLine={false} tickLine={false} />
              <YAxis type="category" dataKey="name" width={60} tick={{ fontSize: 11, fill: "#64748b" }} axisLine={false} tickLine={false} />
              <Tooltip contentStyle={{ borderRadius: 12, border: "1px solid #e2e8f0", fontSize: 12 }} />
              <Bar dataKey="value" fill="#4f46e5" radius={6}>
                {funnelData.map((entry, index) => (
                  <Cell key={`cell-${index}`} fill={entry.color} />
                ))}
              </Bar>
            </BarChart>
          </ResponsiveContainer>
          )}
        </div>

        <div className="of-card p-4">
          <h3 className="mb-4 text-sm font-semibold text-slate-700">状态分布</h3>
          {statusData.length === 0 ? (
            <ChartEmpty text="还没有投递记录，添加后这里会显示各状态占比" />
          ) : (
          <ResponsiveContainer width="100%" height={280}>
            <PieChart>
              <Pie data={statusData} cx="50%" cy="50%" outerRadius={90} innerRadius={50}
                label={({ name, percent = 0 }) => `${name} ${(percent * 100).toFixed(0)}%`}
                labelLine={false} style={{ fontSize: 11 }}
                dataKey="value">
                {statusData.map((entry, index) => (
                  <Cell key={`cell-${index}`} fill={entry.color} />
                ))}
              </Pie>
              <Tooltip contentStyle={{ borderRadius: 12, border: "1px solid #e2e8f0", fontSize: 12 }} />
              <Legend wrapperStyle={{ fontSize: 11 }} />
            </PieChart>
          </ResponsiveContainer>
          )}
        </div>
      </div>
    </div>
  );
}
