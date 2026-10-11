import { useCallback, useEffect, useMemo, useState } from "react";
import { api } from "../../api/client";
import type { AdminUsageResult, AdminTracePage } from "../../types";
import { Card, EmptyState, Icons, Loading, PageHeader, SectionTitle, TableScroll } from "../../components/ui";
import { formatCny } from "../../utils/currency";

const DIMENSIONS = [
  { key: "user", label: "按用户" },
  { key: "operation", label: "按功能" },
  { key: "model", label: "按模型" },
  { key: "day", label: "按日期" },
];

const RANGES = [
  { key: 0, label: "全部" },
  { key: 7, label: "近 7 天" },
  { key: 30, label: "近 30 天" },
  { key: 90, label: "近 90 天" },
];

export default function AdminUsage() {
  const [dimension, setDimension] = useState("user");
  const [days, setDays] = useState(30);
  const [data, setData] = useState<AdminUsageResult | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");

  const [traces, setTraces] = useState<AdminTracePage | null>(null);
  const [page, setPage] = useState(1);
  const [tracesLoading, setTracesLoading] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const res = await api.admin.usage(dimension, days || undefined);
      if (res.success && res.data) setData(res.data);
      else setError(res.error || "加载失败");
    } catch (err) {
      setError(err instanceof Error ? err.message : "加载失败");
    } finally {
      setLoading(false);
    }
  }, [dimension, days]);

  const loadTraces = useCallback(async (p: number) => {
    setTracesLoading(true);
    try {
      const res = await api.admin.traces({ page: p, page_size: 15 });
      if (res.success && res.data) setTraces(res.data);
    } catch {
      /* 明细失败不阻断主视图 */
    } finally {
      setTracesLoading(false);
    }
  }, []);

  useEffect(() => {
    const t = window.setTimeout(() => void load(), 0);
    return () => window.clearTimeout(t);
  }, [load]);

  useEffect(() => {
    const t = window.setTimeout(() => void loadTraces(page), 0);
    return () => window.clearTimeout(t);
  }, [loadTraces, page]);

  // 成本归属拆分：自带 Key（用户自付） vs 平台额度（你承担）
  const costSplit = useMemo(() => {
    if (dimension !== "user" || !data) return null;
    let platform = 0;
    let byok = 0;
    data.items.forEach((i) => {
      if (i.byok) byok += i.cost_cny ?? 0;
      else platform += i.cost_cny ?? 0;
    });
    return { platform, byok };
  }, [data, dimension]);

  const isEmpty = !loading && data && data.items.length === 0;

  return (
    <div>
      <PageHeader title="用量与成本" subtitle="谁在用、花了多少" icon={Icons.chart} />

      {error && <div className="of-alert-error mb-4">{error}</div>}

      {/* 控制条 */}
      <div className="mb-4 flex flex-wrap items-center gap-3">
        <div className="flex gap-1 rounded-xl bg-slate-100 p-1">
          {DIMENSIONS.map((d) => (
            <button
              key={d.key}
              onClick={() => setDimension(d.key)}
              className={`rounded-lg px-3.5 py-1.5 text-sm font-medium transition ${
                dimension === d.key
                  ? "bg-white text-brand-600 shadow-sm"
                  : "text-slate-500 hover:text-slate-700"
              }`}
            >
              {d.label}
            </button>
          ))}
        </div>
        <div className="flex gap-1 rounded-xl bg-slate-100 p-1">
          {RANGES.map((r) => (
            <button
              key={r.key}
              onClick={() => setDays(r.key)}
              className={`rounded-lg px-3 py-1.5 text-xs font-medium transition ${
                days === r.key
                  ? "bg-white text-brand-600 shadow-sm"
                  : "text-slate-500 hover:text-slate-700"
              }`}
            >
              {r.label}
            </button>
          ))}
        </div>
      </div>

      {/* 汇总 */}
      {data?.total && (
        <div className="mb-4 grid grid-cols-3 gap-3">
          <div className="of-card p-4">
            <p className="text-xs text-slate-500">调用次数</p>
            <p className="mt-1 text-xl font-bold text-slate-900">{data.total.calls}</p>
          </div>
          <div className="of-card p-4">
            <p className="text-xs text-slate-500">Token 总量</p>
            <p className="mt-1 text-xl font-bold text-slate-900">{data.total.tokens.toLocaleString()}</p>
          </div>
          <div className="of-card p-4">
            <p className="text-xs text-slate-500">成本合计</p>
            <p className="mt-1 text-xl font-bold text-amber-600">{formatCny(data.total.cost_cny)}</p>
          </div>
        </div>
      )}

      {/* 成本归属（仅按用户维度有意义） */}
      {costSplit && (
        <div className="mb-5 grid grid-cols-1 gap-3 sm:grid-cols-2">
          <div className="of-card border-l-4 border-l-amber-400 p-4">
            <p className="text-xs text-slate-500">平台额度消耗（你承担）</p>
            <p className="mt-1 text-xl font-bold text-amber-600">{formatCny(costSplit.platform)}</p>
            <p className="mt-0.5 text-[11px] text-slate-400">未自带 Key 的用户，走平台默认模型</p>
          </div>
          <div className="of-card border-l-4 border-l-emerald-400 p-4">
            <p className="text-xs text-slate-500">用户自带 Key 消耗（用户自付）</p>
            <p className="mt-1 text-xl font-bold text-emerald-600">{formatCny(costSplit.byok)}</p>
            <p className="mt-0.5 text-[11px] text-slate-400">这部分不花你的钱</p>
          </div>
        </div>
      )}

      <SectionTitle count={data?.items.length}>用量明细</SectionTitle>
      <Card className="mb-6 overflow-hidden p-0">
        {loading ? (
          <Loading text="统计中..." />
        ) : isEmpty ? (
          <div className="p-6">
            <EmptyState
              title="暂无用量数据"
              description="产生 AI 调用后这里会显示统计（若确有使用却为空，请查看「系统健康」）"
            />
          </div>
        ) : (
          <TableScroll>
            <table className="w-full min-w-[720px] text-left text-sm">
              <thead>
                <tr className="border-b border-slate-100 bg-slate-50/80 text-slate-500">
                  <th className="px-5 py-3 font-medium">
                    {DIMENSIONS.find((d) => d.key === dimension)?.label.replace("按", "")}
                  </th>
                  <th className="px-5 py-3 text-right font-medium">调用</th>
                  {dimension === "model" ? (
                    <>
                      <th className="px-5 py-3 text-right font-medium">输入 Token</th>
                      <th className="px-5 py-3 text-right font-medium">输出 Token</th>
                    </>
                  ) : (
                    <th className="px-5 py-3 text-right font-medium">Token</th>
                  )}
                  <th className="px-5 py-3 text-right font-medium">成本</th>
                  <th className="px-5 py-3 text-right font-medium">平均耗时</th>
                  {dimension !== "model" && (
                    <th className="px-5 py-3 text-right font-medium">失败</th>
                  )}
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-50">
                {data?.items.map((row) => (
                  <tr key={row.key} className="transition hover:bg-slate-50/50">
                    <td className="px-5 py-3">
                      {dimension === "user" ? (
                        <div className="min-w-0">
                          <span className="font-medium text-slate-900">{row.name || "(已删除)"}</span>
                          {row.byok && (
                            <span className="ml-2 rounded bg-emerald-50 px-1.5 py-0.5 text-[10px] font-medium text-emerald-600">
                              自带Key
                            </span>
                          )}
                          <span className="block truncate text-xs text-slate-400">
                            {row.email || row.key}
                          </span>
                        </div>
                      ) : dimension === "operation" ? (
                        <span className="font-mono text-xs text-slate-700">{row.key}</span>
                      ) : (
                        <span className="text-slate-700">{row.key}</span>
                      )}
                    </td>
                    <td className="px-5 py-3 text-right text-slate-700">{row.calls}</td>
                    {dimension === "model" ? (
                      <>
                        <td className="px-5 py-3 text-right text-slate-600">
                          {(row.prompt_tokens ?? 0).toLocaleString()}
                        </td>
                        <td className="px-5 py-3 text-right text-slate-600">
                          {(row.completion_tokens ?? 0).toLocaleString()}
                        </td>
                      </>
                    ) : (
                      <td className="px-5 py-3 text-right text-slate-600">
                        {row.tokens.toLocaleString()}
                      </td>
                    )}
                    <td className="px-5 py-3 text-right font-medium text-amber-600">
                      {formatCny(row.cost_cny)}
                    </td>
                    <td className="px-5 py-3 text-right text-slate-500">
                      {row.avg_duration_ms != null ? `${row.avg_duration_ms}ms` : "-"}
                    </td>
                    {dimension !== "model" && (
                      <td className={`px-5 py-3 text-right ${(row.failed ?? 0) > 0 ? "text-rose-600" : "text-slate-400"}`}>
                        {row.failed ?? 0}
                      </td>
                    )}
                  </tr>
                ))}
              </tbody>
            </table>
          </TableScroll>
        )}
      </Card>

      <SectionTitle count={traces?.total}>逐次调用记录</SectionTitle>
      <Card className="overflow-hidden p-0">
        {tracesLoading && !traces ? (
          <Loading text="加载明细..." />
        ) : !traces || traces.items.length === 0 ? (
          <div className="p-6">
            <EmptyState title="暂无调用记录" />
          </div>
        ) : (
          <>
            <TableScroll>
              <table className="w-full min-w-[760px] text-left text-sm">
                <thead>
                  <tr className="border-b border-slate-100 bg-slate-50/80 text-slate-500">
                    <th className="px-5 py-3 font-medium">时间</th>
                    <th className="px-5 py-3 font-medium">用户</th>
                    <th className="px-5 py-3 font-medium">功能</th>
                    <th className="px-5 py-3 font-medium">状态</th>
                    <th className="px-5 py-3 text-right font-medium">Token</th>
                    <th className="px-5 py-3 text-right font-medium">成本</th>
                    <th className="px-5 py-3 text-right font-medium">耗时</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-slate-50">
                  {traces.items.map((t) => (
                    <tr key={t.id} className="transition hover:bg-slate-50/50">
                      <td className="whitespace-nowrap px-5 py-3 text-xs text-slate-500">
                        {t.created_at ? new Date(`${t.created_at}Z`).toLocaleString("zh-CN") : "-"}
                      </td>
                      <td className="px-5 py-3 text-slate-700">{t.user_name || t.user_id || "-"}</td>
                      <td className="px-5 py-3">
                        <span className="font-mono text-xs text-slate-600">{t.operation}</span>
                      </td>
                      <td className="px-5 py-3">
                        <span
                          className={`of-badge ${
                            t.status === "success"
                              ? "bg-emerald-50 text-emerald-600"
                              : "bg-rose-50 text-rose-600"
                          }`}
                        >
                          {t.status === "success" ? "成功" : t.status}
                        </span>
                      </td>
                      <td className="px-5 py-3 text-right text-slate-600">{t.tokens.toLocaleString()}</td>
                      <td className="px-5 py-3 text-right text-amber-600">{formatCny(t.cost_cny)}</td>
                      <td className="px-5 py-3 text-right text-slate-500">{t.duration_ms}ms</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </TableScroll>

            {/* 分页 */}
            <div className="flex items-center justify-between border-t border-slate-100 px-5 py-3">
              <span className="text-xs text-slate-500">
                共 {traces.total} 条 · 第 {traces.page} 页
              </span>
              <div className="flex gap-2">
                <button
                  onClick={() => setPage((p) => Math.max(1, p - 1))}
                  disabled={page <= 1}
                  className="of-btn-ghost text-xs disabled:opacity-40"
                >
                  上一页
                </button>
                <button
                  onClick={() => setPage((p) => p + 1)}
                  disabled={page * traces.page_size >= traces.total}
                  className="of-btn-ghost text-xs disabled:opacity-40"
                >
                  下一页
                </button>
              </div>
            </div>
          </>
        )}
      </Card>
    </div>
  );
}
