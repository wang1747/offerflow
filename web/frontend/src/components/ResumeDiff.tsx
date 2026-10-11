import { type FC, useMemo } from "react";
import type { ChangeItem } from "../types";

interface ResumeDiffProps {
  original?: string;
  optimized?: string;
  changes?: ChangeItem[];
}

type DiffLine = { type: "same" | "add" | "del"; text: string };

/** 基于 LCS 的逐行 diff，把原文和优化文对齐成一条带增删标记的序列。 */
function diffLines(a: string, b: string): DiffLine[] {
  const A = a.split("\n");
  const B = b.split("\n");
  const n = A.length;
  const m = B.length;

  const dp: number[][] = Array.from({ length: n + 1 }, () => new Array(m + 1).fill(0));
  for (let i = n - 1; i >= 0; i--) {
    for (let j = m - 1; j >= 0; j--) {
      dp[i][j] = A[i] === B[j] ? dp[i + 1][j + 1] + 1 : Math.max(dp[i + 1][j], dp[i][j + 1]);
    }
  }

  const out: DiffLine[] = [];
  let i = 0;
  let j = 0;
  while (i < n && j < m) {
    if (A[i] === B[j]) {
      out.push({ type: "same", text: A[i] });
      i++;
      j++;
    } else if (dp[i + 1][j] >= dp[i][j + 1]) {
      out.push({ type: "del", text: A[i] });
      i++;
    } else {
      out.push({ type: "add", text: B[j] });
      j++;
    }
  }
  while (i < n) out.push({ type: "del", text: A[i++] });
  while (j < m) out.push({ type: "add", text: B[j++] });
  return out;
}

const ResumeDiff: FC<ResumeDiffProps> = ({ original, optimized, changes }) => {
  const lines = useMemo(
    () => (original && optimized ? diffLines(original, optimized) : []),
    [original, optimized],
  );

  if (!original && !optimized) {
    return (
      <div className="rounded-xl border border-dashed border-slate-200 p-6 text-center text-sm text-slate-400">
        暂无对比数据
      </div>
    );
  }

  return (
    <div className="space-y-4">
      {changes && changes.length > 0 && (
        <div className="rounded-xl bg-brand-50 p-4">
          <h4 className="mb-3 text-sm font-semibold text-brand-700">AI 改动项（{changes.length}）</h4>
          <ul className="space-y-3">
            {changes.map((c, i) => (
              <li key={i} className="text-sm">
                <div className="mb-1 flex items-center gap-2">
                  <span className="rounded bg-brand-100 px-1.5 py-0.5 text-xs font-medium text-brand-700">
                    {c.section || "正文"}
                  </span>
                  <span className="text-xs text-slate-500">{c.reason}</span>
                </div>
                <div className="rounded-lg bg-white/70 p-2 font-mono text-xs leading-relaxed">
                  <div className="text-rose-600">
                    <span className="mr-1 select-none text-rose-400">-</span>
                    {c.before}
                  </div>
                  <div className="text-emerald-700">
                    <span className="mr-1 select-none text-emerald-500">+</span>
                    {c.after}
                  </div>
                </div>
              </li>
            ))}
          </ul>
        </div>
      )}

      <div className="overflow-hidden rounded-xl border border-slate-200">
        <div className="flex items-center justify-between border-b border-slate-200 bg-slate-50 px-4 py-2 text-sm font-semibold">
          <span className="text-rose-600">优化前</span>
          <span className="text-slate-400">→</span>
          <span className="text-emerald-600">优化后</span>
        </div>
        <div className="max-h-96 overflow-y-auto font-mono text-xs leading-relaxed">
          {lines.length > 0 ? (
            lines.map((line, i) => (
              <div
                key={i}
                className={`flex border-b border-slate-100 last:border-0 ${
                  line.type === "add"
                    ? "bg-emerald-50"
                    : line.type === "del"
                      ? "bg-rose-50"
                      : "bg-white"
                }`}
              >
                <span
                  className={`w-6 flex-shrink-0 select-none px-1 text-right ${
                    line.type === "add"
                      ? "text-emerald-500"
                      : line.type === "del"
                        ? "text-rose-400"
                        : "text-slate-300"
                  }`}
                >
                  {line.type === "add" ? "+" : line.type === "del" ? "-" : " "}
                </span>
                <span
                  className={`flex-1 whitespace-pre-wrap px-2 py-0.5 ${
                    line.type === "add"
                      ? "text-emerald-800"
                      : line.type === "del"
                        ? "text-rose-700 line-through"
                        : "text-slate-600"
                  }`}
                >
                  {line.text || " "}
                </span>
              </div>
            ))
          ) : (
            <div className="grid gap-0 md:grid-cols-2">
              <pre className="whitespace-pre-wrap bg-rose-50/50 p-4 text-slate-700">{original}</pre>
              <pre className="whitespace-pre-wrap border-t border-slate-200 bg-emerald-50/50 p-4 text-slate-700 md:border-l md:border-t-0">
                {optimized}
              </pre>
            </div>
          )}
        </div>
      </div>
    </div>
  );
};

export default ResumeDiff;
