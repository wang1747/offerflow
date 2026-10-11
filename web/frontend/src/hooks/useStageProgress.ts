import { useEffect, useState } from "react";

/**
 * 长耗时 AI 操作的分阶段进度提示。
 * 在 `active` 期间按 `intervalMs` 轮换展示 `stages` 里的文字，
 * 让用户感知「任务在推进」而非卡住；结束后自动复位。
 */
export function useStageProgress(
  stages: string[],
  active: boolean,
  intervalMs = 4000,
): string {
  const [index, setIndex] = useState(0);

  useEffect(() => {
    if (!active) {
      setIndex(0);
      return;
    }
    if (stages.length <= 1) {
      setIndex(0);
      return;
    }
    const timer = setInterval(() => {
      setIndex((i) => (i + 1) % stages.length);
    }, intervalMs);
    return () => clearInterval(timer);
  }, [active, stages.length, intervalMs]);

  return stages[index] || "";
}
