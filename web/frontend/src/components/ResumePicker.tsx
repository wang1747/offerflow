import { useRef, useState } from "react";
import { api } from "../api/client";
import type { ResumeItem } from "../types";
import { resumeLabel } from "../utils/resume";

const ACCEPT = ".pdf,.docx,.md,.markdown,.txt";

interface ResumePickerProps {
  /** 当前选中的简历 id */
  value: string;
  onChange: (id: string) => void;
  /** 已有简历列表（由父组件维护，便于各页保留自己的过滤规则） */
  resumes: ResumeItem[];
  /** 上传成功后重新拉取列表，让新简历出现在下拉里 */
  onRefresh?: () => void | Promise<void>;
  label?: string;
  /** 无简历时的引导文案 */
  emptyHint?: string;
  disabled?: boolean;
  className?: string;
}

/**
 * 简历选择器：下拉选已有 + 直接上传新简历（上传后自动选中）。
 *
 * 存在的理由：以前「简历」只能从「简历优化」页进入系统，面试准备 / 薪资参考 /
 * 岗位分析都假设简历已经存在，新账号进去就只能干瞪眼。这里把「就近补录」补齐，
 * 后端 resume_id 仍保持必填（由前端保证一定选得出来）。
 */
export function ResumePicker({
  value,
  onChange,
  resumes,
  onRefresh,
  label = "简历",
  emptyHint = "还没有简历 —— 上传一份就能用，其他模块也会自动看到它",
  disabled = false,
  className = "",
}: ResumePickerProps) {
  const fileRef = useRef<HTMLInputElement>(null);
  const [uploading, setUploading] = useState(false);
  const [error, setError] = useState("");
  const [okMsg, setOkMsg] = useState("");

  const handleFile = async (files: FileList | null) => {
    const file = files && files[0];
    if (!file) return;
    setError("");
    setOkMsg("");
    setUploading(true);
    try {
      const res = await api.resume.uploadFile(file);
      if (res.success && res.data?.id) {
        // 先刷新列表再选中，避免出现「值是新的、选项里还没有」的空档
        await onRefresh?.();
        onChange(res.data.id);
        setOkMsg(`已上传「${res.data.filename || file.name}」并选中`);
      } else {
        setError(res.error || "上传失败");
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : "上传失败");
    } finally {
      setUploading(false);
      // 清空 input，否则同一个文件第二次选不会触发 change
      if (fileRef.current) fileRef.current.value = "";
    }
  };

  const pick = () => fileRef.current?.click();

  return (
    <div className={className}>
      {resumes.length > 0 && (
        <div className="mb-1.5 flex items-center justify-between">
          <label className="text-xs font-medium text-slate-500">{label}</label>
          <span className="text-[11px] text-slate-400">共 {resumes.length} 份</span>
        </div>
      )}

      {resumes.length > 0 ? (
        <div className="flex gap-2">
          <select
            className="of-select flex-1"
            value={value}
            onChange={(e) => onChange(e.target.value)}
            disabled={disabled}
            aria-label={label}
          >
            <option value="">选择简历</option>
            {resumes.map((resume) => (
              <option key={resume.id} value={resume.id}>
                {resumeLabel(resume)}
              </option>
            ))}
          </select>
          <button
            type="button"
            onClick={pick}
            disabled={disabled || uploading}
            className="of-btn-outline whitespace-nowrap"
          >
            {uploading ? "上传中..." : "上传简历"}
          </button>
        </div>
      ) : (
        <div className="rounded-xl border border-dashed border-slate-300 bg-slate-50/60 p-3">
          <p className="text-xs leading-relaxed text-slate-500">{emptyHint}</p>
          <button
            type="button"
            onClick={pick}
            disabled={disabled || uploading}
            className="of-btn-primary mt-2 w-full py-1.5 text-sm"
          >
            {uploading ? "上传中..." : "上传简历（PDF / Word / MD / TXT）"}
          </button>
        </div>
      )}

      <input
        ref={fileRef}
        type="file"
        accept={ACCEPT}
        className="hidden"
        onChange={(e) => handleFile(e.target.files)}
        aria-label="上传简历文件"
      />

      {error && <p className="mt-1.5 text-xs text-rose-600">{error}</p>}
      {okMsg && <p className="mt-1.5 text-xs text-emerald-600">{okMsg}</p>}
    </div>
  );
}

export default ResumePicker;
