import { useState } from "react";
import { api } from "../api/client";
import type { JDItem } from "../types";

const jdLabel = (jd: JDItem) =>
  `${jd.company || "未知公司"} · ${jd.position || "未知职位"}`;

interface JdPickerProps {
  /** 当前选中的 JD id */
  value: string;
  onChange: (id: string) => void;
  /** 已有 JD 列表（父组件维护） */
  jds: JDItem[];
  /** 解析成功后重新拉取列表 */
  onRefresh?: () => void | Promise<void>;
  label?: string;
  disabled?: boolean;
  className?: string;
}

/**
 * 岗位选择器：下拉选已有 + 直接粘贴 JD 解析（解析后自动选中）。
 *
 * JD 本身就是纯文本、不存在「格式」问题，所以这里用粘贴是合理的
 * （简历则一律走文件上传，不提供粘贴入口）。
 */
export function JdPicker({
  value,
  onChange,
  jds,
  onRefresh,
  label = "岗位",
  disabled = false,
  className = "",
}: JdPickerProps) {
  const [open, setOpen] = useState(false);
  const [text, setText] = useState("");
  const [parsing, setParsing] = useState(false);
  const [error, setError] = useState("");
  const [okMsg, setOkMsg] = useState("");

  const handleParse = async () => {
    const trimmed = text.trim();
    setError("");
    setOkMsg("");
    if (!trimmed) {
      setError("请先粘贴职位要求");
      return;
    }
    setParsing(true);
    try {
      const res = await api.jd.parse(trimmed);
      if (res.success && res.data?.id) {
        await onRefresh?.();
        onChange(res.data.id);
        setText("");
        setOpen(false);
        const parsed = res.data.parsed;
        const hasField = parsed && Object.keys(parsed).length > 0;
        setOkMsg(
          hasField
            ? "已解析并选中该岗位"
            : "已保存，但未解析出结构化字段，请检查 JD 内容",
        );
      } else {
        setError(res.error || "解析失败");
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : "解析失败");
    } finally {
      setParsing(false);
    }
  };

  return (
    <div className={className}>
      {jds.length > 0 && (
        <div className="mb-1.5 flex items-center justify-between">
          <label className="text-xs font-medium text-slate-500">{label}</label>
          <span className="text-[11px] text-slate-400">共 {jds.length} 个</span>
        </div>
      )}

      <div className="flex gap-2">
        <select
          className="of-select flex-1"
          value={value}
          onChange={(e) => onChange(e.target.value)}
          disabled={disabled}
          aria-label={label}
        >
          <option value="">{jds.length ? "选择岗位" : "暂无岗位"}</option>
          {jds.map((jd) => (
            <option key={jd.id} value={jd.id}>
              {jdLabel(jd)}
            </option>
          ))}
        </select>
        <button
          type="button"
          onClick={() => {
            setError("");
            setOkMsg("");
            setOpen((v) => !v);
          }}
          disabled={disabled}
          className="of-btn-outline whitespace-nowrap"
        >
          {open ? "收起" : "粘贴 JD"}
        </button>
      </div>

      {open && (
        <div className="mt-2 rounded-xl border border-slate-200 bg-slate-50/60 p-3">
          <textarea
            className="of-textarea h-28 resize-none"
            placeholder="粘贴招聘要求文本，AI 会解析出公司、职位、硬性要求与技能栈..."
            value={text}
            onChange={(e) => setText(e.target.value)}
            disabled={parsing}
            aria-label="岗位要求文本"
          />
          <button
            type="button"
            onClick={handleParse}
            disabled={parsing || !text.trim()}
            className="of-btn-primary mt-2 w-full py-1.5 text-sm"
          >
            {parsing ? "解析中..." : "解析并选中"}
          </button>
        </div>
      )}

      {error && <p className="mt-1.5 text-xs text-rose-600">{error}</p>}
      {okMsg && <p className="mt-1.5 text-xs text-emerald-600">{okMsg}</p>}
    </div>
  );
}

export default JdPicker;
