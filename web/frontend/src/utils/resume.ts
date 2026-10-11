import type { ResumeItem } from "../types";

/** 简历下拉的统一标签：区分基础简历 / 优化版 / AI 生成，让每条都能被认出来。 */
export function resumeLabel(resume: ResumeItem): string {
  const pj = resume.parsed_json;
  if (pj?.kind === "generated") {
    const pos = pj.generation?.position || pj.generation?.name;
    return pos ? `${pos} · v${resume.version}` : `AI 生成 · v${resume.version}`;
  }
  if (pj?.kind === "optimized") {
    const tj = pj.target_jd;
    const target = tj?.company || tj?.position
      ? `${tj.company || ""}${tj.company && tj.position ? " · " : ""}${tj.position || ""}`
      : "未指定岗位";
    return `优化版 · ${target} · v${resume.version}`;
  }
  return resume.source_file || "手动输入";
}
