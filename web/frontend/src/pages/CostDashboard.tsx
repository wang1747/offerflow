import { useCallback, useEffect, useRef, useState, type ReactNode } from "react";
import {
  BarChart, Bar, XAxis, YAxis, CartesianGrid, Tooltip, ResponsiveContainer, PieChart, Pie, Cell,
} from "recharts";
import { api } from "../api/client";
import { Card, EmptyState, Icons, Loading } from "../components/ui";
import TraceWaterfall from "../components/TraceWaterfall";
import type { CostSummary, TraceItem, TraceDetail } from "../types";
import { formatCny } from "../utils/currency";

const OPERATION_LABELS: Record<string, string> = {
  jd_parse: "JD 解析",
  resume_optimize: "简历优化",
  interview_prep: "面试题生成",
  mock_interview_summary: "面试总结",
  mock_interview_question: "面试提问",
  mock_interview_feedback: "面试反馈",
  article_extract: "面经解析",
  resume_generate: "简历生成",
  resume_generate_retry: "简历生成重试",
  resume_condense: "简历精简",
  resume_generate_condense: "简历生成精简",
  resume_optimize_extract: "简历优化提取",
  resume_regenerate_section: "简历逐段重写",
  resume_regenerate_variants: "简历改写候选",
  salary_reference_extract: "薪资画像提取",
  salary_reference_analysis: "薪资解读",
  negotiation_hr: "谈判 HR",
  negotiation_coach: "谈判教练",
  negotiation_summary: "谈判总结",
  salary_benchmark_refresh: "薪资基准刷新",
};

const OPERATION_COLORS: Record<string, string> = {
  jd_parse: "#4f46e5",
  resume_optimize: "#8b5cf6",
  interview_prep: "#06b6d4",
  mock_interview_summary: "#14b8a6",
  mock_interview_question: "#3b82f6",
  mock_interview_feedback: "#f59e0b",
  article_extract: "#f97316",
  resume_generate: "#6366f1",
  resume_generate_retry: "#818cf8",
  resume_condense: "#a78bfa",
  resume_generate_condense: "#c084fc",
  resume_optimize_extract: "#d946ef",
  resume_regenerate_section: "#ec4899",
  resume_regenerate_variants: "#f43f5e",
  salary_reference_extract: "#0ea5e9",
  salary_reference_analysis: "#10b981",
  negotiation_hr: "#eab308",
  negotiation_coach: "#ef4444",
  negotiation_summary: "#64748b",
  salary_benchmark_refresh: "#22c55e",
};

const MODEL_COLORS = ["#4f46e5", "#8b5cf6", "#06b6d4", "#14b8a6", "#f59e0b", "#ef4444", "#3b82f6"];

const formatDuration = (ms: number) => {
  if (ms < 1000) return `${ms}ms`;
  if (ms < 60000) return `${(ms / 1000).toFixed(1)}s`;
  return `${Math.floor(ms / 60000)}m ${Math.round((ms % 60000) / 1000)}s`;
};
const formatDate = (iso?: string | null) => {
  if (!iso) return "-";
  const d = new Date(iso);
  return `${d.toLocaleDateString()} ${d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}`;
};

/** 数字滚动动画 */
function useCountUp(end: number, duration = 700) {
  const [val, setVal] = useState(0);
  const frame = useRef(0);
  useEffect(() => {
    const start = performance.now();
    const tick = (now: number) => {
      const p = Math.min((now - start) / duration, 1);
      setVal(p * end);
      if (p < 1) frame.current = requestAnimationFrame(tick);
    };
    frame.current = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(frame.current);
  }, [end, duration]);
  return val;
}

/** 高级统计卡片 */
function HeroStat({
  label,
  value,
  sub,
  gradient,
  icon,
}: {
  label: string;
  value: ReactNode;
  sub: string;
  gradient: string;
  icon: ReactNode;
}) {
  return (
    <div className="group relative overflow-hidden rounded-2xl border border-slate-200/70 bg-white p-5 shadow-card transition-all duration-300 hover:-translate-y-0.5 hover:border-slate-300 hover:shadow-hover">
      <div className={`pointer-events-none absolute -right-8 -top-8 h-28 w-28 rounded-full bg-gradient-to-br ${gradient} opacity-[0.07] blur-2xl transition-opacity duration-300 group-hover:opacity-[0.14]`} />
      <div className="flex items-start justify-between">
        <div>
          <p className="text-xs font-medium uppercase tracking-wider text-slate-400">{label}</p>
          <p className="mt-2 text-3xl font-bold tabular-nums tracking-tight text-slate-900">{value}</p>
          <p className="mt-1 text-xs text-slate-400">{sub}</p>
        </div>
        <div className={`flex h-11 w-11 items-center justify-center rounded-xl bg-gradient-to-br ${gradient} text-white shadow-lg`}>
          {icon}
        </div>
      </div>
    </div>
  );
}

/** 卡片标题 */
function CardHeader({ icon, title, desc, gradient }: { icon: ReactNode; title: string; desc: string; gradient: string }) {
  return (
    <div className="mb-5 flex items-center gap-3">
      <div className={`flex h-9 w-9 items-center justify-center rounded-lg bg-gradient-to-br ${gradient} text-white shadow-md [&>svg]:h-full [&>svg]:w-full`}>
        {icon}
      </div>
      <div>
        <h3 className="text-sm font-semibold text-slate-900">{title}</h3>
        <p className="text-xs text-slate-400">{desc}</p>
      </div>
    </div>
  );
}

export default function CostDashboard() {
  const [summary, setSummary] = useState<CostSummary | null>(null);
  const [traces, setTraces] = useState<TraceItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [expandedId, setExpandedId] = useState<string | null>(null);
  const [spanDetail, setSpanDetail] = useState<TraceDetail | null>(null);
  const [spanLoading, setSpanLoading] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const [sumRes, tracesRes] = await Promise.all([
        api.observability.summary(),
        api.observability.traces(1, 50),
      ]);
      if (sumRes.success && sumRes.data) setSummary(sumRes.data);
      if (tracesRes.success && tracesRes.data) setTraces(tracesRes.data);
    } catch (err) {
      console.error("加载成本数据失败:", err);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const toggleTrace = async (id: string) => {
    if (expandedId === id) {
      setExpandedId(null);
      setSpanDetail(null);
      return;
    }
    setExpandedId(id);
    setSpanLoading(true);
    setSpanDetail(null);
    try {
      const res = await api.observability.traceDetail(id);
      if (res.success && res.data) setSpanDetail(res.data);
    } catch (err) {
      console.error("加载 trace 详情失败:", err);
    } finally {
      setSpanLoading(false);
    }
  };

  const totalCost = summary?.total_cost_cny ?? 0;
  const totalTokens = summary?.total_tokens ?? 0;
  const traceCount = summary?.trace_count ?? 0;

  const costAnim = useCountUp(totalCost);
  const tokenAnim = useCountUp(totalTokens);

  if (loading) {
    return <Loading text="加载成本数据中..." />;
  }

  const opData = (summary?.by_operation ?? []).map((o) => ({
    name: OPERATION_LABELS[o.operation] || o.operation,
    cost: o.cost_cny ?? 0,
    count: o.count,
    fill: OPERATION_COLORS[o.operation] || "#94a3b8",
  }));
  const modelData = (summary?.by_model ?? []).map((m, i) => ({
    name: m.model,
    value: m.cost_cny ?? 0,
    fill: MODEL_COLORS[i % MODEL_COLORS.length],
  }));

  const noData = !summary || traceCount === 0;

  return (
    <div className="animate-fade-in">
      {/* 页头 */}
      <div className="mb-6 flex items-center justify-between">
        <div className="flex items-center gap-3">
          <div className="flex h-11 w-11 items-center justify-center rounded-xl bg-gradient-to-br from-indigo-500 to-violet-500 text-white shadow-lg shadow-indigo-500/25">
            {Icons.chart}
          </div>
          <div>
            <h1 className="text-2xl font-bold tracking-tight text-slate-900">成本看板</h1>
            <p className="mt-0.5 text-sm text-slate-500">每一次 AI 调用的 token 与费用明细</p>
          </div>
        </div>
        <button onClick={() => void load()} className="of-btn-outline">
          刷新
        </button>
      </div>

      {/* 统计卡片 */}
      <div className="mb-6 grid grid-cols-1 gap-4 sm:grid-cols-3">
        <HeroStat
          label="累计费用"
          value={formatCny(costAnim)}
          sub="估算值 · 按参考汇率折算"
          gradient="from-indigo-500 to-violet-500"
          icon={<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><line x1="12" y1="1" x2="12" y2="23" /><path d="M17 5H9.5a3.5 3.5 0 0 0 0 7h5a3.5 3.5 0 0 1 0 7H6" /></svg>}
        />
        <HeroStat
          label="累计 Token"
          value={Math.round(tokenAnim).toLocaleString()}
          sub="输入 + 输出"
          gradient="from-sky-500 to-cyan-500"
          icon={<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><polyline points="22 12 16 12 14 15 10 15 8 12 2 12" /><path d="M5.45 5.11 2 12v6a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2v-6l-3.45-6.89A2 2 0 0 0 16.76 4H7.24a2 2 0 0 0-1.79 1.11z" /></svg>}
        />
        <HeroStat
          label="调用次数"
          value={traceCount.toLocaleString()}
          sub="共录得 trace 数"
          gradient="from-emerald-500 to-teal-500"
          icon={<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M13 2 3 14h9l-1 8 10-12h-9l1-8z" /></svg>}
        />
      </div>

      {noData ? (
        <Card className="p-8">
          <EmptyState
            icon={Icons.chart}
            title="还没有成本数据"
            description="使用 JD 解析、简历优化、面试准备等功能后，这里会自动统计每次 AI 调用的费用"
          />
        </Card>
      ) : (
        <>
          {/* 图表 */}
          <div className="mb-6 grid gap-4 lg:grid-cols-5">
            <Card className="p-5 lg:col-span-3">
              <CardHeader icon={Icons.chart} title="按功能分布" desc="各功能的费用分布" gradient="from-indigo-500 to-violet-500" />
              {opData.length > 0 ? (
                <ResponsiveContainer width="100%" height={280}>
                  <BarChart data={opData} layout="vertical" barCategoryGap="28%">
                    <CartesianGrid strokeDasharray="3 3" stroke="#f1f5f9" horizontal={false} />
                    <XAxis type="number" tick={{ fontSize: 11, fill: "#94a3b8" }} axisLine={false} tickLine={false} />
                    <YAxis type="category" dataKey="name" width={82} tick={{ fontSize: 12, fill: "#475569" }} axisLine={false} tickLine={false} />
                    <Tooltip
                      contentStyle={{ borderRadius: 14, border: "1px solid #e2e8f0", boxShadow: "0 8px 24px rgba(15,23,42,0.08)", fontSize: 12 }}
                      cursor={{ fill: "rgba(99,102,241,0.04)" }}
                      formatter={(value) => [formatCny(Number(value ?? 0)), "费用"]}
                    />
                    <Bar dataKey="cost" radius={[0, 8, 8, 0]} barSize={18}>
                      {opData.map((entry, index) => (
                        <Cell key={`cell-${index}`} fill={entry.fill} />
                      ))}
                    </Bar>
                  </BarChart>
                </ResponsiveContainer>
              ) : (
                <EmptyState title="暂无分布数据" />
              )}
            </Card>

            <Card className="p-5 lg:col-span-2">
              <CardHeader icon={Icons.track} title="按模型占比" desc="各模型费用占比" gradient="from-violet-500 to-fuchsia-500" />
              {modelData.length > 0 ? (
                <div className="relative">
                  <ResponsiveContainer width="100%" height={280}>
                    <PieChart>
                      <Pie
                        data={modelData}
                        cx="50%"
                        cy="50%"
                        outerRadius={92}
                        innerRadius={56}
                        dataKey="value"
                        paddingAngle={2}
                        cornerRadius={4}
                        strokeWidth={0}
                      >
                        {modelData.map((entry, index) => (
                          <Cell key={`cell-${index}`} fill={entry.fill} />
                        ))}
                      </Pie>
                      <Tooltip
                        contentStyle={{ borderRadius: 14, border: "1px solid #e2e8f0", boxShadow: "0 8px 24px rgba(15,23,42,0.08)", fontSize: 12 }}
                        formatter={(value) => [formatCny(Number(value ?? 0)), "费用"]}
                      />
                    </PieChart>
                  </ResponsiveContainer>
                  <div className="pointer-events-none absolute inset-0 flex flex-col items-center justify-center">
                    <span className="text-[11px] text-slate-400">总费用</span>
                    <span className="text-lg font-bold tabular-nums text-slate-900">{formatCny(totalCost)}</span>
                  </div>
                </div>
              ) : (
                <EmptyState title="暂无模型数据" />
              )}
              <div className="mt-3 flex flex-wrap gap-x-4 gap-y-1.5">
                {modelData.slice(0, 5).map((m) => (
                  <span key={m.name} className="inline-flex items-center gap-1.5 text-xs text-slate-500">
                    <span className="h-2 w-2 rounded-full" style={{ backgroundColor: m.fill }} />
                    <span className="max-w-[120px] truncate">{m.name}</span>
                  </span>
                ))}
              </div>
            </Card>
          </div>

          {/* 调用记录 */}
          <Card className="p-5">
            <CardHeader icon={Icons.track} title="最近调用记录" desc="每次操作的 LLM 调用明细" gradient="from-slate-500 to-slate-700" />
            {traces.length === 0 ? (
              <EmptyState title="暂无记录" />
            ) : (
              <div className="space-y-1.5">
                {traces.map((t) => {
                  const open = expandedId === t.id;
                  return (
                    <div key={t.id} className={`overflow-hidden rounded-xl border transition-colors ${open ? "border-brand-200 bg-brand-50/30" : "border-slate-100 hover:border-slate-200"}`}>
                      <button
                        onClick={() => void toggleTrace(t.id)}
                        className="flex w-full items-center gap-3 px-4 py-3 text-left transition hover:bg-slate-50/70"
                      >
                        <span className={`h-2 w-2 flex-shrink-0 rounded-full ${t.status === "failed" ? "bg-rose-500" : "bg-emerald-500"}`} />
                        <span className="min-w-0 flex-1">
                          <span className="block truncate text-sm font-medium text-slate-900">
                            {OPERATION_LABELS[t.operation] || t.operation}
                          </span>
                          <span className="mt-0.5 block text-xs text-slate-400">{formatDate(t.created_at)}</span>
                        </span>
                        <span className="hidden flex-shrink-0 text-xs tabular-nums text-slate-500 sm:block">
                          {t.total_tokens.toLocaleString()} tok
                        </span>
                        <span className="hidden flex-shrink-0 text-xs tabular-nums text-slate-400 md:block">
                          {formatDuration(t.duration_ms)}
                        </span>
                        <span className="flex-shrink-0 text-sm font-semibold tabular-nums text-brand-600">
                          {formatCny(t.total_cost_cny ?? 0)}
                        </span>
                        <svg
                          width="16" height="16" viewBox="0 0 16 16" fill="none"
                          className={`flex-shrink-0 text-slate-400 transition-transform duration-200 ${open ? "rotate-180" : ""}`}
                        >
                          <path d="M4 6l4 4 4-4" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" />
                        </svg>
                      </button>

                      {open && (
                        <div className="border-t border-brand-100/60 bg-white/60 px-4 py-3">
                          {spanLoading ? (
                            <Loading text="加载明细..." />
                          ) : spanDetail && spanDetail.spans.length > 0 ? (
                            <div className="space-y-4">
                              <div>
                                <p className="mb-2 text-xs font-medium text-slate-500">调用链时间线</p>
                                <TraceWaterfall spans={spanDetail.spans} totalDurationMs={spanDetail.trace.duration_ms} />
                              </div>
                              <div className="overflow-x-auto">
                              <table className="w-full text-xs">
                                <thead>
                                  <tr className="text-left text-slate-400">
                                    <th className="py-1.5 pr-3 font-medium">模型</th>
                                    <th className="py-1.5 pr-3 text-right font-medium">输入</th>
                                    <th className="py-1.5 pr-3 text-right font-medium">输出</th>
                                    <th className="py-1.5 pr-3 text-right font-medium">耗时</th>
                                    <th className="py-1.5 text-right font-medium">费用</th>
                                  </tr>
                                </thead>
                                <tbody>
                                  {spanDetail.spans.map((s) => (
                                    <tr key={s.id} className="border-t border-slate-100 text-slate-600">
                                      <td className="py-1.5 pr-3 font-mono">{s.model || "未知"}</td>
                                      <td className="py-1.5 pr-3 text-right tabular-nums">{s.prompt_tokens.toLocaleString()}</td>
                                      <td className="py-1.5 pr-3 text-right tabular-nums">{s.completion_tokens.toLocaleString()}</td>
                                      <td className="py-1.5 pr-3 text-right">{formatDuration(s.duration_ms)}</td>
                                      <td className="py-1.5 text-right font-medium tabular-nums text-brand-600">
                                        {s.cost_cny === null ? "未知" : formatCny(s.cost_cny)}
                                      </td>
                                    </tr>
                                  ))}
                                </tbody>
                              </table>
                              </div>
                            </div>
                          ) : (
                            <p className="text-xs text-slate-400">暂无明细</p>
                          )}
                        </div>
                      )}
                    </div>
                  );
                })}
              </div>
            )}
          </Card>
        </>
      )}
    </div>
  );
}
