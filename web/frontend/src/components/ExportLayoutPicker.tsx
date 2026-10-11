import { useEffect, useState } from "react";

import { api } from "../api/client";
import type { ExportLayoutChoice } from "../types";

interface ExportLayoutPickerProps {
  /** 当前版式 key（`single` / `split`）。 */
  value: string;
  /** 回调带上栏数：调用方据此决定 Word 按钮是否可用，不必自己再判一遍 key。 */
  onChange: (key: string, columns: number) => void;
  className?: string;
}

/**
 * 导出「版式」选择器（单栏 / 双栏）。
 *
 * 选项文案来自后端 `/api/v1/resume/layouts`，页面里不再手抄一份 ——
 * 后端加版式时前端不用跟着改，也不会出现「前端有选项、后端不认」的错位。
 *
 * 清单拉取失败时**显式**降级：渲染一个禁用的「单栏（清单加载失败）」并给出
 * title 说明。刻意不做成「拉不到就不渲染控件」——那样用户会以为这份简历
 * 根本没有版式选项，而实际原因只是这次请求挂了。
 */
export default function ExportLayoutPicker({
  value,
  onChange,
  className,
}: ExportLayoutPickerProps) {
  const [choices, setChoices] = useState<ExportLayoutChoice[]>([]);
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    let alive = true;
    api.resume
      .layouts()
      .then((res) => {
        if (!alive) return;
        if (res.success && res.data && res.data.length > 0) {
          setChoices(res.data);
        } else {
          setFailed(true);
        }
      })
      .catch(() => {
        if (alive) setFailed(true);
      });
    return () => {
      alive = false;
    };
  }, []);

  const current = choices.find((item) => item.key === value);

  return (
    <label className={`flex items-center gap-1.5 text-xs text-slate-500 ${className || ""}`}>
      版式
      <select
        className="of-select py-1 text-xs"
        value={value}
        onChange={(event) => {
          const next = choices.find((item) => item.key === event.target.value);
          onChange(event.target.value, next?.columns ?? 1);
        }}
        disabled={choices.length === 0}
        title={current?.desc || (failed ? "版式清单加载失败，本次将按单栏导出" : undefined)}
      >
        {choices.length === 0 && (
          <option value="single">{failed ? "单栏（清单加载失败）" : "单栏"}</option>
        )}
        {choices.map((item) => (
          <option key={item.key} value={item.key}>
            {item.label}
          </option>
        ))}
      </select>
    </label>
  );
}
