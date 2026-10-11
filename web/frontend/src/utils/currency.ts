/**
 * 金额格式化。
 *
 * 成本底层以美元计价 —— 模型厂商（DeepSeek / OpenAI / Kimi）官方都按美元/百万 token 标价，
 * 所以 `Trace.total_cost` / `Span.cost` 存的都是美元原值，用于和厂商账单逐笔对齐。
 * 面向用户的展示统一用人民币，折算由后端按参考汇率完成
 * （见 backend/app/observability/cost.py 的 USD_TO_CNY），前端只负责排版。
 */

/** 人民币（带 ¥）：不足 1 元保留 4 位小数，否则保留 2 位 */
export function formatCny(v: number | null | undefined): string {
  const n = typeof v === "number" && Number.isFinite(v) ? v : 0;
  if (n === 0) return "¥0.00";
  return n < 1 ? `¥${n.toFixed(4)}` : `¥${n.toFixed(2)}`;
}

/** 人民币数值（不带 ¥），供需要自行拼接符号的场景使用 */
export function cnyNumber(v: number | null | undefined): string {
  const n = typeof v === "number" && Number.isFinite(v) ? v : 0;
  if (n === 0) return "0.00";
  return n < 1 ? n.toFixed(4) : n.toFixed(2);
}

/** 美元金额（带 $），管理后台对照厂商账单时使用 */
export function formatUsd(v: number | null | undefined, digits = 4): string {
  const n = typeof v === "number" && Number.isFinite(v) ? v : 0;
  return `$${n.toFixed(digits)}`;
}
