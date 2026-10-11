import type { SpanItem } from "../types";

const formatDuration = (ms: number) => {
  if (ms < 1000) return `${ms}ms`;
  if (ms < 60000) return `${(ms / 1000).toFixed(1)}s`;
  return `${Math.floor(ms / 60000)}m ${Math.round((ms % 60000) / 1000)}s`;
};

/**
 * 调用链瀑布图：横轴为时间，每个 Span 是一条时间条，
 * 左侧偏移 = start_offset_ms，宽度 = duration_ms，直观呈现各次 LLM 调用的时序与耗时。
 */
export default function TraceWaterfall({
  spans,
  totalDurationMs,
}: {
  spans: SpanItem[];
  totalDurationMs: number;
}) {
  const total = Math.max(
    totalDurationMs,
    ...spans.map((s) => s.start_offset_ms + s.duration_ms),
    1,
  );

  const ticks = [0, 0.25, 0.5, 0.75, 1];

  return (
    <div>
      {/* 时间轴刻度 */}
      <div className="relative mb-2 ml-28 h-6 border-b border-slate-100 text-[10px] text-slate-400">
        {ticks.map((p) => (
          <span
            key={p}
            className="absolute -translate-x-1/2 tabular-nums"
            style={{ left: `${p * 100}%` }}
          >
            {formatDuration(total * p)}
          </span>
        ))}
      </div>

      {/* 每个 Span 一行 */}
      {spans.map((s, idx) => {
        const left = (s.start_offset_ms / total) * 100;
        const width = Math.max((s.duration_ms / total) * 100, 0.6);
        const failed = s.status === "error" || s.status === "failed";
        return (
          <div key={s.id} className="group mb-1.5 flex items-center gap-3">
            {/* 模型名 */}
            <div className="w-28 flex-shrink-0 truncate text-right font-mono text-xs text-slate-500">
              {s.model || "未知"}
            </div>

            {/* 时间条轨道 */}
            <div className="relative h-6 flex-1 rounded-md bg-slate-50">
              {/* 网格线（4 等分） */}
              {[0.25, 0.5, 0.75].map((p) => (
                <span
                  key={p}
                  className="absolute top-0 h-full w-px bg-slate-100"
                  style={{ left: `${p * 100}%` }}
                />
              ))}
              {/* 时间条 */}
              <div
                className={`absolute top-1 bottom-1 rounded-md transition-all ${
                  failed
                    ? "bg-gradient-to-r from-rose-400 to-rose-500"
                    : idx % 2 === 0
                      ? "bg-gradient-to-r from-indigo-500 to-violet-500"
                      : "bg-gradient-to-r from-sky-500 to-cyan-500"
                } shadow-sm group-hover:brightness-110`}
                style={{ left: `${left}%`, width: `${width}%` }}
                title={`${s.model || "未知"} · ${formatDuration(s.duration_ms)} · ${s.total_tokens} tokens`}
              />
            </div>

            {/* 耗时 */}
            <div className="w-16 flex-shrink-0 text-right text-xs tabular-nums text-slate-400">
              {formatDuration(s.duration_ms)}
            </div>
          </div>
        );
      })}

      {/* 图例 */}
      <div className="mt-2 ml-28 flex items-center gap-4 text-[10px] text-slate-400">
        <span className="flex items-center gap-1">
          <span className="h-2 w-2 rounded-full bg-gradient-to-r from-indigo-500 to-violet-500" />
          正常调用
        </span>
        <span className="flex items-center gap-1">
          <span className="h-2 w-2 rounded-full bg-rose-400" />
          失败
        </span>
      </div>
    </div>
  );
}
