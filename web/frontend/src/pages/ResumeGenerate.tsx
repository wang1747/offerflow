import { type ChangeEvent, type FC, useCallback, useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../api/client";
import type {
  JDItem,
  ResumeItem,
  ResumeGenerationResult,
  ResumeGenerationEducation,
  ResumeGenerationExperience,
} from "../types";
import { Card, PageHeader, SectionTitle, Badge, Icons } from "../components/ui";
import ExportLayoutPicker from "../components/ExportLayoutPicker";
import { JOB_TEMPLATES } from "../jobTemplates";
import { useStageProgress } from "../hooks/useStageProgress";

const experienceTypeLabel = (type: string) => {
  if (type === "project") return "项目";
  if (type === "internship") return "实习";
  if (type === "work") return "工作";
  return "经历";
};

// 质量维度展示顺序（与后端 quality.py 的 dimensions 键一致）
const DIMENSION_ORDER = ["verb", "quantification", "result", "filler", "ats", "length", "one_page"];

// AI 语义评分维度（与后端 semantic.py 的键一致）
const SEMANTIC_DIMS = [
  { key: "result_credibility", label: "结果可信度" },
  { key: "expression_quality", label: "表达质量" },
  { key: "jd_fit", label: "岗位契合度" },
] as const;

const contactText = (contact?: ResumeGenerationResult["contact"]) =>
  [contact?.phone, contact?.email, contact?.github].filter(Boolean).join(" | ");

const emptyEducation = (): ResumeGenerationEducation => ({
  school: "",
  major: "",
  degree: "",
  start: "",
  end: "",
  courses: "",
  gpa: "",
  honors: "",
});

const emptyExperience = (): ResumeGenerationExperience => ({
  type: "project",
  name: "",
  role: "",
  start: "",
  end: "",
  bullets: [""],
});

// 学历选项（下拉）
const DEGREE_OPTIONS = ["高中及以下", "中专", "大专", "本科", "硕士", "博士"];

/** 标签输入：回车/逗号提交，返回处理函数 */
const ResumeGenerate: FC = () => {
  const [jds, setJds] = useState<JDItem[]>([]);
  const [selectedJdId, setSelectedJdId] = useState("");

  const [name, setName] = useState("");
  const [position, setPosition] = useState("");
  const [phone, setPhone] = useState("");
  const [email, setEmail] = useState("");
  const [github, setGithub] = useState("");
  const [summary, setSummary] = useState("");

  const [direction, setDirection] = useState("tech");

  const [educations, setEducations] = useState<ResumeGenerationEducation[]>([emptyEducation()]);
  const [experiences, setExperiences] = useState<ResumeGenerationExperience[]>([emptyExperience()]);
  const [skills, setSkills] = useState<string[]>([]);
  const [skillInput, setSkillInput] = useState("");
  const [certifications, setCertifications] = useState<string[]>([]);
  const [certInput, setCertInput] = useState("");

  const [jdText, setJdText] = useState("");
  const [generating, setGenerating] = useState(false);
  const [result, setResult] = useState<ResumeGenerationResult | null>(null);
  const [history, setHistory] = useState<ResumeItem[]>([]);
  // 当前结果所属简历的版本历史（同一份简历每次变更都追加新版本，旧版本不可变）
  const [versions, setVersions] = useState<ResumeItem[]>([]);
  const [error, setError] = useState("");
  // 导出/优化界面用的版式（单栏 / 双栏），沿用上一次选择。
  // layoutColumns 由选择器回传，用来决定 Word 按钮是否可用（Word 不支持多栏）。
  const [exportLayout, setExportLayout] = useState("single");
  const [layoutColumns, setLayoutColumns] = useState(1);
  const [candidates, setCandidates] = useState<unknown[]>([]);
  const [candidateInfo, setCandidateInfo] = useState<{ section: string; index?: number } | null>(null);
  const [candidatesLoading, setCandidatesLoading] = useState(false);

  const generateStage = useStageProgress(
    ["正在分析岗位要求…", "正在撰写个人总结…", "正在展开项目经历…", "正在润色与校验事实…"],
    generating,
  );

  const contactParts = result ? contactText(result.contact) : "";
  const template = JOB_TEMPLATES.find((t) => t.key === direction) ?? JOB_TEMPLATES[JOB_TEMPLATES.length - 1];
  const recommendedSkills = template.recommendedSkills;
  const quality = result?.quality;
  const qualityDimensions = quality?.dimensions;
  const qualityViolations = quality?.violations ?? [];
  const semantic = result?.semantic;
  const sufficiency = result?.sufficiency;

  const loadJds = useCallback(async () => {
    try {
      const resp = await api.jd.list();
      if (resp.success && resp.data) setJds(resp.data);
    } catch { setJds([]); }
  }, []);

  const loadHistory = useCallback(async (): Promise<ResumeItem[]> => {
    try {
      const resp = await api.resume.list();
      if (!resp.success || !resp.data) return [];
      const items = resp.data
        .filter((r) => r.parsed_json?.kind === "generated" && r.parsed_json?.generation)
        .sort((a, b) => (b.version || 0) - (a.version || 0));
      setHistory(items);
      return items;
    } catch { /* 忽略，加载失败不阻塞页面 */ return []; }
  }, []);

  useEffect(() => { void loadJds(); }, [loadJds]);
  useEffect(() => {
    // 进入页面只加载历史列表，默认不展示任何结果
    void loadHistory();
  }, [loadHistory]);

  // 拉取「当前结果所属简历」的版本历史：传任意一个版本 id 都能取到整份文档的全部版本
  const loadVersions = useCallback(async (resumeId?: string) => {
    if (!resumeId) { setVersions([]); return; }
    try {
      const resp = await api.resume.versions(resumeId);
      if (resp.success && resp.data) setVersions(resp.data);
    } catch { /* 版本历史拉取失败不影响主流程 */ }
  }, []);

  const handleJdSelect = (e: ChangeEvent<HTMLSelectElement>) => {
    const id = e.target.value;
    setSelectedJdId(id);
    const jd = jds.find((item) => item.id === id);
    setJdText(jd?.raw_text || "");
  };

  const handleJdTextChange = (e: ChangeEvent<HTMLTextAreaElement>) => {
    setJdText(e.target.value);
    setSelectedJdId("");
  };

  // ===== 教育经历 =====
  const updateEducation = (index: number, field: keyof ResumeGenerationEducation, value: string) =>
    setEducations((prev) => prev.map((item, i) => (i === index ? { ...item, [field]: value } : item)));
  const addEducation = () => setEducations((prev) => [...prev, emptyEducation()]);
  const removeEducation = (index: number) =>
    setEducations((prev) => prev.filter((_, i) => i !== index));

  // ===== 项目与实习经历 =====
  const updateExperience = (index: number, field: keyof ResumeGenerationExperience, value: string) =>
    setExperiences((prev) => prev.map((item, i) => (i === index ? { ...item, [field]: value } : item)));
  const updateBullet = (index: number, bulletIndex: number, value: string) =>
    setExperiences((prev) =>
      prev.map((item, i) =>
        i === index
          ? { ...item, bullets: item.bullets.map((b, bi) => (bi === bulletIndex ? value : b)) }
          : item,
      ),
    );
  const addBullet = (index: number) =>
    setExperiences((prev) =>
      prev.map((item, i) => (i === index ? { ...item, bullets: [...item.bullets, ""] } : item)),
    );
  const removeBullet = (index: number, bulletIndex: number) =>
    setExperiences((prev) =>
      prev.map((item, i) =>
        i === index ? { ...item, bullets: item.bullets.filter((_, bi) => bi !== bulletIndex) } : item,
      ),
    );
  const addExperience = () => setExperiences((prev) => [...prev, emptyExperience()]);
  const removeExperience = (index: number) =>
    setExperiences((prev) => prev.filter((_, i) => i !== index));

  // ===== 技能 / 证书标签 =====
  const commitSkill = () => {
    const value = skillInput.trim().replace(/[,，、]$/, "");
    if (value && !skills.includes(value)) setSkills((prev) => [...prev, value]);
    setSkillInput("");
  };
  const handleSkillKeyDown = (e: React.KeyboardEvent<HTMLInputElement>) => {
    if (e.key === "Enter" || e.key === ",") {
      e.preventDefault();
      commitSkill();
    }
  };
  const removeSkill = (value: string) => setSkills((prev) => prev.filter((s) => s !== value));

  const commitCert = () => {
    const value = certInput.trim().replace(/[,，、]$/, "");
    if (value && !certifications.includes(value)) setCertifications((prev) => [...prev, value]);
    setCertInput("");
  };
  const handleCertKeyDown = (e: React.KeyboardEvent<HTMLInputElement>) => {
    if (e.key === "Enter" || e.key === ",") {
      e.preventDefault();
      commitCert();
    }
  };
  const removeCert = (value: string) => setCertifications((prev) => prev.filter((c) => c !== value));

  const handleGenerate = async () => {
    setError("");
    setResult(null);
    if (!name.trim() || !position.trim()) {
      setError("请填写姓名和求职意向");
      return;
    }
    const edu = educations.filter((e) => e.school.trim() || e.major.trim());
    const exps = experiences.filter((e) => e.name.trim());
    if (edu.length === 0 && exps.length === 0) {
      setError("请至少填写一段教育经历或项目/实习经历");
      return;
    }

    setGenerating(true);
    try {
      const resp = await api.resumeGeneration.generate({
        name: name.trim(),
        position: position.trim(),
        phone: phone.trim() || undefined,
        email: email.trim() || undefined,
        github: github.trim() || undefined,
        summary: summary.trim() || undefined,
        direction: template.label,
        education: edu,
        experiences: exps,
        skills,
        certifications,
        jd_text: jdText.trim() || undefined,
        jd_id: selectedJdId || undefined,
      });
      if (resp.success && resp.data) {
        setResult(resp.data);
        void loadHistory(); // 刷新生成历史列表
        void loadVersions(resp.data.id); // 新结果是新文档的 v1
      } else {
        setError(resp.error || "生成失败");
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : "生成失败");
    } finally {
      setGenerating(false);
    }
  };

  const handleExport = async (format: "pdf" | "word") => {
    if (!result) return;
    try {
      const blob = await api.resume.export(result.id, format, exportLayout);
      const url = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = url;
      link.download = `resume-${result.id}.${format === "pdf" ? "pdf" : "docx"}`;
      document.body.appendChild(link);
      link.click();
      document.body.removeChild(link);
      URL.revokeObjectURL(url);
    } catch (err) {
      setError(err instanceof Error ? err.message : "导出失败");
    }
  };

  const buildStructured = (r: ResumeGenerationResult) => ({
    name: r.name,
    position: r.position,
    contact: r.contact,
    summary: r.summary,
    education: r.education,
    experiences: r.experiences,
    skills: r.skills,
    certifications: r.certifications,
  });

  const handleRegenerate = async (section: string, index?: number) => {
    if (!result) return;
    setError("");
    setCandidates([]);
    setCandidateInfo(null);
    setCandidatesLoading(true);
    try {
      const resp = await api.resumeGeneration.regenerateSectionVariants({
        resume_id: result.id,
        structured: buildStructured(result),
        section,
        index,
        jd_text: jdText.trim() || undefined,
        jd_id: selectedJdId || undefined,
      });
      if (resp.success && resp.data?.candidates?.length) {
        setCandidates(resp.data.candidates);
        setCandidateInfo({ section, index });
      } else {
        setError(resp.error || "生成候选失败");
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : "生成候选失败");
    } finally {
      setCandidatesLoading(false);
    }
  };

  const applyCandidate = async (candidate: unknown) => {
    if (!result || !candidateInfo) return;
    const structured = buildStructured(result) as Record<string, unknown>;
    const { section, index } = candidateInfo;
    if (section === "summary") {
      structured.summary = candidate;
    } else if (section === "skills") {
      structured.skills = candidate;
    } else if (section === "certifications") {
      structured.certifications = candidate;
    } else if (section === "experiences" && index !== undefined) {
      const exps = structured.experiences as unknown[];
      if (exps) exps[index] = candidate;
    } else if (section === "education" && index !== undefined) {
      const edus = structured.education as unknown[];
      if (edus) edus[index] = candidate;
    }
    try {
      const resp = await api.resumeGeneration.save({
        resume_id: result.id,
        structured,
        jd_text: jdText.trim() || undefined,
        jd_id: selectedJdId || undefined,
      });
      if (resp.success && resp.data) {
        setResult(resp.data);
        setCandidates([]);
        setCandidateInfo(null);
        // 保存候选 = 追加新版本 → 刷新版本历史与生成历史
        void loadVersions(resp.data.id);
        void loadHistory();
      } else {
        setError(resp.error || "保存失败");
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : "保存失败");
    }
  };

  const handleDeleteHistory = async (item: ResumeItem) => {
    const label = item.parsed_json?.generation?.position || item.parsed_json?.generation?.name || "";
    if (!window.confirm(`确定删除这条生成记录「${label} · v${item.version}」吗？删除后不可恢复。`)) return;
    try {
      const resp = await api.resume.delete(item.id);
      if (resp.success) {
        setHistory((prev) => prev.filter((r) => r.id !== item.id));
        if (result?.id === item.id) setResult(null);
      } else {
        setError(resp.error || "删除失败");
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : "删除失败");
    }
  };

  return (
    <div>
      <PageHeader
        title="简历生成"
        subtitle="逐项填写真实信息，AI 生成一页以内、匹配目标岗位的中文简历"
        icon={Icons.rocket}
      />

      {error && <div className="of-alert-error mb-4">{Icons.alert}<span>{error}</span></div>}

      <div className="grid grid-cols-1 gap-5 lg:grid-cols-3">
        {/* ===== 左：结构化输入 ===== */}
        <Card className="p-5 lg:col-span-2">
          <SectionTitle>求职方向</SectionTitle>
          <div className="flex flex-wrap gap-2">
            {JOB_TEMPLATES.map((t) => (
              <button
                key={t.key}
                type="button"
                onClick={() => setDirection(t.key)}
                className={`rounded-full px-3 py-1.5 text-sm transition ${
                  direction === t.key
                    ? "bg-brand-600 text-white"
                    : "bg-slate-100 text-slate-600 hover:bg-slate-200"
                }`}
              >
                {t.label}
              </button>
            ))}
          </div>

          <div className="mt-5">
            <SectionTitle>基本信息</SectionTitle>
            <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
              <input className="of-input" placeholder="姓名 *" value={name} onChange={(e) => setName(e.target.value)} />
              <input className="of-input" placeholder="求职意向，如：Python 后端实习 *" value={position} onChange={(e) => setPosition(e.target.value)} />
              <input className="of-input" placeholder="手机号" value={phone} onChange={(e) => setPhone(e.target.value)} />
              <input className="of-input" placeholder="邮箱" value={email} onChange={(e) => setEmail(e.target.value)} />
              <input className="of-input sm:col-span-2" placeholder="作品集 / 博客链接（可选）" value={github} onChange={(e) => setGithub(e.target.value)} />
            </div>
          </div>
          {/* 教育经历 */}
          <div className="mt-6">
            <SectionTitle
              count={educations.length}
              right={
                <button type="button" onClick={addEducation} className="of-btn-outline px-2 py-1 text-xs">
                  + 添加教育经历
                </button>
              }
            >
              教育经历
            </SectionTitle>
            <p className="mb-2 text-xs text-slate-400">学校、专业、学历会原样写入简历，请如实填写；未填写的内容不会自动补全</p>
            <div className="space-y-3">
              {educations.map((edu, index) => (
                <div key={index} className="rounded-xl border border-slate-200 p-3">
                  <div className="grid grid-cols-2 gap-2 sm:grid-cols-3">
                    <input className="of-input" placeholder="学校" value={edu.school} onChange={(e) => updateEducation(index, "school", e.target.value)} />
                    <input className="of-input" placeholder="专业" value={edu.major} onChange={(e) => updateEducation(index, "major", e.target.value)} />
                    <select
                      className={`of-select ${edu.degree ? "text-slate-900" : "text-slate-400"}`}
                      value={edu.degree}
                      onChange={(e) => updateEducation(index, "degree", e.target.value)}
                    >
                      <option value="">学历</option>
                      {DEGREE_OPTIONS.map((d) => (
                        <option key={d} value={d}>{d}</option>
                      ))}
                    </select>
                    <input className="of-input" placeholder="开始（如 2023-09）" value={edu.start} onChange={(e) => updateEducation(index, "start", e.target.value)} />
                    <input className="of-input" placeholder="结束（如 2027-06）" value={edu.end} onChange={(e) => updateEducation(index, "end", e.target.value)} />
                    <div className="flex items-center justify-end">
                      {educations.length > 1 && (
                        <button type="button" onClick={() => removeEducation(index)} className="text-xs text-rose-500 hover:underline">
                          删除
                        </button>
                      )}
                    </div>
                  </div>
                  <div className="mt-2 grid grid-cols-1 gap-2 sm:grid-cols-3">
                    <input className="of-input" placeholder="主修课程（可选）" value={edu.courses ?? ""} onChange={(e) => updateEducation(index, "courses", e.target.value)} />
                    <input className="of-input" placeholder="绩点（可选，如 3.6/4.0）" value={edu.gpa ?? ""} onChange={(e) => updateEducation(index, "gpa", e.target.value)} />
                    <input className="of-input" placeholder="荣誉 / 获奖（可选）" value={edu.honors ?? ""} onChange={(e) => updateEducation(index, "honors", e.target.value)} />
                  </div>
                </div>
              ))}
            </div>
          </div>

          {/* 项目与实习经历 */}
          <div className="mt-6">
            <SectionTitle
              count={experiences.length}
              right={
                <button type="button" onClick={addExperience} className="of-btn-outline px-2 py-1 text-xs">
                  + 添加一段经历
                </button>
              }
            >
              项目与实习经历
            </SectionTitle>
            <p className="mb-2 text-xs text-slate-400">项目/公司名、角色、时间会原样写入简历，请如实填写；未填写的内容不会自动补全</p>
            <div className="mb-3 rounded-lg bg-brand-50/60 p-3">
              <div className="mb-1 text-xs font-medium text-brand-700">示例要点（可参考，按需替换）</div>
              <ul className="space-y-1">
                {template.exampleBullets.map((b, i) => (
                  <li key={i} className="text-xs text-slate-600">{b}</li>
                ))}
              </ul>
            </div>
            <div className="space-y-3">
              {experiences.map((exp, index) => (
                <div key={index} className="rounded-xl border border-slate-200 p-3">
                  <div className="flex items-center gap-2">
                    <select
                      className="of-select w-28"
                      value={exp.type}
                      onChange={(e) => updateExperience(index, "type", e.target.value)}
                    >
                      <option value="project">项目</option>
                      <option value="internship">实习</option>
                      <option value="work">工作</option>
                    </select>
                    <input className="of-input flex-1" placeholder="项目 / 公司名" value={exp.name} onChange={(e) => updateExperience(index, "name", e.target.value)} />
                    {experiences.length > 1 && (
                      <button type="button" onClick={() => removeExperience(index)} className="text-xs text-rose-500 hover:underline">
                        删除
                      </button>
                    )}
                  </div>
                  <div className="mt-2 grid grid-cols-1 gap-2 sm:grid-cols-3">
                    <input className="of-input" placeholder="角色 / 职位" value={exp.role} onChange={(e) => updateExperience(index, "role", e.target.value)} />
                    <input className="of-input" placeholder="开始（如 2026-03）" value={exp.start} onChange={(e) => updateExperience(index, "start", e.target.value)} />
                    <input className="of-input" placeholder="结束（如 2026-08）" value={exp.end} onChange={(e) => updateExperience(index, "end", e.target.value)} />
                  </div>
                  <div className="mt-2 space-y-1.5">
                    {exp.bullets.map((bullet, bulletIndex) => (
                      <div key={bulletIndex} className="flex items-center gap-2">
                        <span className="text-xs text-slate-300">•</span>
                        <input
                          className="of-input flex-1"
                          placeholder={template.bulletHint}
                          value={bullet}
                          onChange={(e) => updateBullet(index, bulletIndex, e.target.value)}
                        />
                        {exp.bullets.length > 1 && (
                          <button type="button" onClick={() => removeBullet(index, bulletIndex)} className="text-xs text-slate-400 hover:text-rose-500">
                            ×
                          </button>
                        )}
                      </div>
                    ))}
                    <button type="button" onClick={() => addBullet(index)} className="text-xs text-brand-600 hover:underline">
                      + 添加要点
                    </button>
                  </div>
                </div>
              ))}
            </div>
          </div>

          {/* 技能 */}
          <div className="mt-6">
            <SectionTitle>技能</SectionTitle>
            {recommendedSkills.length > 0 && (
              <div className="mb-2 flex flex-wrap gap-1.5">
                {recommendedSkills.map((s) => {
                  const added = skills.includes(s);
                  return (
                    <button
                      key={s}
                      type="button"
                      onClick={() => !added && setSkills((prev) => [...prev, s])}
                      className={`rounded-full px-2.5 py-1 text-xs transition ${
                        added
                          ? "bg-slate-100 text-slate-400 line-through"
                          : "bg-slate-100 text-slate-600 hover:bg-brand-50 hover:text-brand-700"
                      }`}
                    >
                      {added ? s : `+ ${s}`}
                    </button>
                  );
                })}
              </div>
            )}
            <div className="flex flex-wrap items-center gap-1.5 rounded-xl border border-slate-200 p-2">
              {skills.map((skill) => (
                <span key={skill} className="inline-flex items-center gap-1 rounded-full bg-brand-50 px-2.5 py-1 text-xs text-brand-700">
                  {skill}
                  <button type="button" onClick={() => removeSkill(skill)} className="text-brand-400 hover:text-brand-600">×</button>
                </span>
              ))}
              <input
                className="min-w-[140px] flex-1 border-0 bg-transparent p-1 text-sm outline-none"
                placeholder="输入技能后回车"
                value={skillInput}
                onChange={(e) => setSkillInput(e.target.value)}
                onKeyDown={handleSkillKeyDown}
                onBlur={commitSkill}
              />
            </div>
          </div>

          {/* 证书 */}
          <div className="mt-6">
            <SectionTitle>证书（可选）</SectionTitle>
            <div className="flex flex-wrap items-center gap-1.5 rounded-xl border border-slate-200 p-2">
              {certifications.map((cert) => (
                <span key={cert} className="inline-flex items-center gap-1 rounded-full bg-cyan-50 px-2.5 py-1 text-xs text-cyan-700">
                  {cert}
                  <button type="button" onClick={() => removeCert(cert)} className="text-cyan-400 hover:text-cyan-600">×</button>
                </span>
              ))}
              <input
                className="min-w-[140px] flex-1 border-0 bg-transparent p-1 text-sm outline-none"
                placeholder="输入证书后回车"
                value={certInput}
                onChange={(e) => setCertInput(e.target.value)}
                onKeyDown={handleCertKeyDown}
                onBlur={commitCert}
              />
            </div>
          </div>

          {/* 个人总结 / 自我评价 */}
          <div className="mt-6">
            <SectionTitle>个人总结（可选）</SectionTitle>
            <textarea
              className="of-textarea h-24"
              placeholder="2~3 句总结你的核心优势，留空则由 AI 生成"
              value={summary}
              onChange={(e) => setSummary(e.target.value)}
            />
          </div>
        </Card>

        {/* ===== 右：目标岗位 ===== */}
        <Card className="flex flex-col p-5">
          <SectionTitle>目标岗位</SectionTitle>
          <select className="of-select" value={selectedJdId} onChange={handleJdSelect}>
            <option value="">手动粘贴岗位要求</option>
            {jds.map((item) => (
              <option key={item.id} value={item.id}>
                {item.company || "未知公司"} · {item.position || "未知职位"}
              </option>
            ))}
          </select>
          <textarea
            className="of-textarea mt-3 min-h-56 flex-1"
            placeholder="粘贴目标岗位的招聘要求..."
            value={jdText}
            onChange={handleJdTextChange}
          />
          <button onClick={handleGenerate} disabled={generating} className="of-btn-primary mt-3 w-full py-2">
            {generating ? (
              <><span className="h-4 w-4 animate-spin rounded-full border-2 border-white/30 border-t-white" />{generateStage}</>
            ) : <>{Icons.rocket} 生成简历</>}
          </button>
        </Card>
      </div>

      {/* ===== 生成历史（独立入口） ===== */}
      {history.length > 0 && (
        <div className="mt-5 rounded-xl border border-slate-200 p-4">
          <div className="mb-2 flex items-center gap-2">
            <span className="text-sm font-medium text-slate-700">生成历史</span>
            <span className="text-xs text-slate-400">点击查看，再点收起</span>
          </div>
          <div className="flex flex-wrap gap-2">
            {history.map((item) => {
              const g = item.parsed_json?.generation;
              if (!g) return null;
              const active = result?.id === g.id;
              return (
                <div
                  key={item.id}
                  className={`flex items-center overflow-hidden rounded-full text-xs transition ${
                    active
                      ? "bg-brand-600 text-white"
                      : "bg-slate-100 text-slate-600 hover:bg-slate-200"
                  }`}
                >
                  <button
                    type="button"
                    onClick={() => {
                      setResult(active ? null : g);
                      void loadVersions(active ? undefined : g.id);
                    }}
                    className="px-3 py-1"
                  >
                    {g.position || g.name} · v{g.version}
                  </button>
                  <button
                    type="button"
                    onClick={() => handleDeleteHistory(item)}
                    className={`px-1.5 py-1 ${
                      active ? "text-white/70 hover:text-white" : "text-slate-400 hover:text-rose-500"
                    }`}
                    title="删除这条记录"
                  >
                    ×
                  </button>
                </div>
              );
            })}
          </div>
          {versions.length > 1 && (
            <div className="mt-3 border-t border-slate-100 pt-2 text-xs text-slate-500">
              <span className="mr-1">当前简历版本：</span>
              {versions.map((v) => (
                <span
                  key={v.id}
                  className={v.id === result?.id ? "mx-1 font-medium text-brand-600" : "mx-1"}
                >
                  v{v.version}
                  {v.id === result?.id ? "（当前）" : ""}
                </span>
              ))}
              <span className="ml-1 text-slate-400">共 {versions.length} 版，历史版本不可修改</span>
            </div>
          )}
        </div>
      )}

      {/* ===== 结果 ===== */}
      {result && (
        <Card className="mt-5 p-5">
          <SectionTitle>
            生成结果
          </SectionTitle>

          {sufficiency && sufficiency.level !== "sufficient" && (
            <div className="mb-3 rounded-lg border border-sky-200 bg-sky-50/60 p-3">
              <div className="mb-1.5 flex items-center gap-1.5">
                <span className="text-xs font-semibold text-sky-800">信息充足度 {sufficiency.score}</span>
                <span className="text-xs text-sky-600">{sufficiency.message}</span>
              </div>
              {sufficiency.gaps?.length > 0 && (
                <ul className="space-y-1">
                  {sufficiency.gaps.map((g, i) => (
                    <li key={i} className="text-xs text-slate-600">· {g}</li>
                  ))}
                </ul>
              )}
            </div>
          )}

          <div className="mb-3 grid grid-cols-1 gap-3 md:grid-cols-3">
            <div className="rounded-lg bg-emerald-50 p-3">
              <div className="text-xs font-medium text-emerald-800">质量评分</div>
              <div className="mt-0.5 flex items-center gap-2">
                <span className="text-xl font-bold text-emerald-700">
                  {quality?.total_score != null
                    ? quality.total_score
                    : result.ats?.score != null
                      ? result.ats.score
                      : "待评分"}
                </span>
                {quality && (
                  quality.passed ? <Badge color="green">达标</Badge> : <Badge color="amber">待优化</Badge>
                )}
              </div>
              {quality?.total_score != null && result.ats?.score != null && (
                <div className="mt-1 text-[11px] text-emerald-600">ATS 兼容分 {result.ats.score}</div>
              )}
            </div>
            <div className="rounded-lg bg-slate-50 p-3">
              <div className="text-xs font-medium text-slate-700">已命中 JD 关键词</div>
              <div className="mt-1 flex flex-wrap gap-1.5">
                {result.gap?.matched?.length
                  ? result.gap.matched.map((item) => <Badge key={item} color="green">{item}</Badge>)
                  : <span className="text-xs text-slate-400">无</span>}
              </div>
            </div>
            <div className="rounded-lg bg-amber-50 p-3">
              <div className="text-xs font-medium text-amber-800">缺失 JD 关键词</div>
              <div className="mt-1 flex flex-wrap gap-1.5">
                {result.gap?.missing?.length
                  ? result.gap.missing.map((item) => <Badge key={item} color="amber">{item}</Badge>)
                  : <span className="text-xs text-slate-400">无</span>}
              </div>
            </div>
          </div>

          {/* 质量维度评分（7 维度条形） */}
          {qualityDimensions && (
            <div className="mb-3 rounded-lg border border-slate-200 p-3">
              <div className="mb-2 flex items-center gap-1.5">
                <span className="text-xs font-semibold text-slate-700">质量维度</span>
                {quality?.passed ? <Badge color="green">已达标</Badge> : <Badge color="amber">待优化</Badge>}
                <span className="ml-auto text-[11px] text-slate-400">满分 100</span>
              </div>
              <div className="grid grid-cols-1 gap-x-4 gap-y-2 sm:grid-cols-2">
                {DIMENSION_ORDER.map((key) => {
                  const d = qualityDimensions[key];
                  if (!d) return null;
                  const bar = d.score >= 80 ? "bg-emerald-500" : d.score >= 60 ? "bg-amber-500" : "bg-rose-500";
                  return (
                    <div key={key}>
                      <div className="flex items-center justify-between text-xs">
                        <span className="text-slate-600">{d.label}</span>
                        <span className="font-medium text-slate-700">{d.score}</span>
                      </div>
                      <div className="mt-1 h-1.5 w-full overflow-hidden rounded-full bg-slate-100">
                        <div className={`h-1.5 rounded-full ${bar}`} style={{ width: `${Math.min(100, d.score)}%` }} />
                      </div>
                    </div>
                  );
                })}
              </div>
            </div>
          )}

          {/* AI 语义评分（软质量，与正则硬指标分开） */}
          {semantic && (semantic.result_credibility || semantic.expression_quality || semantic.jd_fit) && (
            <div className="mb-3 rounded-lg border border-violet-200 bg-violet-50/50 p-3">
              <div className="mb-2 flex items-center gap-1.5">
                <span className="text-xs font-semibold text-violet-800">AI 语义评分</span>
                {semantic.total_score != null && <Badge color="blue">{semantic.total_score}</Badge>}
                <span className="ml-auto text-[11px] text-violet-400">软质量 · AI 理解语义</span>
              </div>
              <div className="space-y-1.5">
                {SEMANTIC_DIMS.map(({ key, label }) => {
                  const d = semantic?.[key];
                  if (!d) return null;
                  return (
                    <div key={key} className="flex items-start gap-2 text-xs leading-relaxed">
                      <span className="flex-shrink-0 rounded bg-violet-200/70 px-1.5 py-0.5 font-medium text-violet-800">
                        {label} {d.score}
                      </span>
                      <span className="text-slate-600">{d.reason}</span>
                    </div>
                  );
                })}
              </div>
              {semantic.overall && <div className="mt-2 text-xs text-slate-500">{semantic.overall}</div>}
            </div>
          )}

          {/* 质量改进建议（程序化检测，定位到具体 bullet） */}
          {qualityViolations.length > 0 && (
            <div className="mb-3 rounded-lg border border-amber-200 bg-amber-50/60 p-3">
              <div className="mb-1.5 flex items-center gap-1.5">
                {Icons.alert}
                <span className="text-xs font-semibold text-amber-800">质量改进建议</span>
                <span className="text-xs text-amber-600">逐条定位，可针对性重写</span>
              </div>
              <ul className="space-y-1.5">
                {qualityViolations.map((v, i) => (
                  <li key={i} className="flex items-start gap-2 text-xs leading-relaxed text-slate-600">
                    <span className="mt-0.5 flex-shrink-0 rounded bg-amber-200/70 px-1.5 py-0.5 font-medium text-amber-800">
                      {v.section}
                    </span>
                    <span>
                      {v.issue}
                      {v.suggestion && <span className="text-slate-500">　→ {v.suggestion}</span>}
                    </span>
                  </li>
                ))}
              </ul>
            </div>
          )}

          {/* 事实保真信任面板 */}
          {result.fidelity && (
            <div className={`mb-3 flex items-center gap-2 rounded-lg px-3 py-1.5 text-xs ${result.fidelity.passed ? "bg-emerald-50" : "bg-amber-50"}`}>
              {result.fidelity.passed ? (
                <>
                  <span className="flex items-center gap-1 font-medium text-emerald-700">{Icons.check} 事实保真校验通过</span>
                  <span className="text-emerald-600">学校、公司、技能、证书、联系方式均来自你的填写，未发现编造</span>
                </>
              ) : (
                <>
                  <span className="flex items-center gap-1 font-medium text-amber-700">{Icons.alert} 检测到疑似新增内容，请核对</span>
                  <span className="text-amber-700">
                    {[...result.fidelity.added_schools, ...result.fidelity.added_companies, ...result.fidelity.added_skills, ...result.fidelity.added_certifications, ...result.fidelity.added_contacts].join("、")}
                  </span>
                </>
              )}
            </div>
          )}

          {result.reference_source && (
            <div className="mb-3 flex items-center gap-2 rounded-lg bg-slate-50 px-3 py-1.5 text-xs text-slate-500">
              <span className="text-slate-400">参考素材</span>
              <span>本文写法参考了{result.reference_source}，内容仍完全基于你填写的真实信息</span>
            </div>
          )}

          {/* 改写候选选择 */}
          {candidates.length > 0 && (
            <div className="mb-3 rounded-lg border border-brand-200 bg-brand-50/50 p-3">
              <div className="mb-1.5 flex items-center gap-2">
                <span className="text-xs font-semibold text-brand-700">选择改写版本</span>
                {candidatesLoading && <span className="text-xs text-slate-400">生成中...</span>}
                <button type="button" onClick={() => { setCandidates([]); setCandidateInfo(null); }} className="ml-auto text-xs text-slate-400 hover:text-slate-600">取消</button>
              </div>
              <div className="space-y-2">
                {candidates.map((c, i) => {
                  const preview = typeof c === "string"
                    ? c
                    : Array.isArray(c)
                      ? c.join("、")
                      : c && typeof c === "object"
                        ? (() => {
                            const o = c as Record<string, unknown>;
                            const head = (o.name ?? o.school ?? o.position ?? "") as string;
                            const bullets = Array.isArray(o.bullets) ? (o.bullets as string[]).join("；") : "";
                            return [head, bullets].filter(Boolean).join("：");
                          })()
                        : String(c);
                  return (
                    <div key={i} className="flex items-start gap-3 rounded-lg bg-white p-3">
                      <div className="min-w-0 flex-1 text-sm leading-relaxed text-slate-700">{preview}</div>
                      <button type="button" onClick={() => applyCandidate(c)} className="of-btn-primary flex-shrink-0 px-3 py-1.5 text-xs">选用</button>
                    </div>
                  );
                })}
              </div>
            </div>
          )}

          {result.tips?.length > 0 && (
            <div className="mb-4 flex flex-wrap gap-1.5">
              {result.tips.map((tip) => (
                <Badge key={tip} color="amber">{tip}</Badge>
              ))}
            </div>
          )}

          {result.risks && result.risks.length > 0 && (
            <div className="mb-3 rounded-lg border border-amber-200 bg-amber-50/60 p-3">
              <div className="mb-1.5 flex items-center gap-1.5">
                {Icons.alert}
                <span className="text-xs font-semibold text-amber-800">简历风险提示</span>
                <span className="text-xs text-amber-600">对标投标「废标陷阱」，投递前建议逐条核对</span>
              </div>
              <ul className="space-y-1.5">
                {result.risks.map((r, i) => (
                  <li key={i} className="flex items-start gap-2 text-xs leading-relaxed text-slate-600">
                    <span className="mt-0.5 flex-shrink-0 rounded bg-amber-200/70 px-1.5 py-0.5 font-medium text-amber-800">
                      {r.section}
                    </span>
                    <span>
                      {r.issue}
                      {r.suggestion && <span className="text-slate-500">　→ {r.suggestion}</span>}
                    </span>
                  </li>
                ))}
              </ul>
            </div>
          )}

          <div className="rounded-xl bg-slate-50 p-5">
            <div className="flex flex-wrap items-baseline justify-between gap-3">
              <h2 className="text-lg font-semibold text-slate-900">{result.name}</h2>
              <span className="text-sm font-medium text-emerald-700">{result.position}</span>
            </div>

            {contactParts && (
              <div className="mt-2 text-xs text-slate-500">
                {contactParts}
              </div>
            )}

            {result.summary && (
              <section className="mt-5">
                <div className="flex items-center justify-between">
                  <h3 className="text-sm font-semibold text-slate-800">个人总结</h3>
                  <button type="button" onClick={() => handleRegenerate("summary")} className="of-btn-outline px-2 py-1 text-xs">
                    重新生成
                  </button>
                </div>
                <p className="mt-2 text-sm leading-relaxed text-slate-700">{result.summary}</p>
              </section>
            )}

            {result.education?.length > 0 && (
              <section className="mt-5">
                <div className="flex items-center justify-between">
                  <h3 className="text-sm font-semibold text-slate-800">教育经历</h3>
                  <button type="button" onClick={() => handleRegenerate("education")} className="of-btn-outline px-2 py-1 text-xs">
                    重新生成
                  </button>
                </div>
                <div className="mt-2 space-y-2">
                  {result.education.map((edu, index) => (
                    <div key={index}>
                      <div className="flex items-center justify-between gap-3">
                        <div className="text-sm font-medium text-slate-800">
                          {[edu.school, edu.major, edu.degree].filter(Boolean).join(" ")}
                          {(edu.start || edu.end) && (
                            <span className="ml-2 text-xs font-normal text-slate-400">
                              {[edu.start, edu.end].filter(Boolean).join(" ~ ")}
                            </span>
                          )}
                        </div>
                        <button type="button" onClick={() => handleRegenerate("education", index)} className="of-btn-outline px-2 py-1 text-xs">
                          重生成
                        </button>
                      </div>
                      {edu.detail && <div className="text-xs text-slate-500">{edu.detail}</div>}
                    </div>
                  ))}
                </div>
              </section>
            )}

            {result.experiences?.length > 0 && (
              <section className="mt-5">
                <div className="flex items-center justify-between">
                  <h3 className="text-sm font-semibold text-slate-800">项目与经历</h3>
                  <button type="button" onClick={() => handleRegenerate("experiences")} className="of-btn-outline px-2 py-1 text-xs">
                    重新生成
                  </button>
                </div>
                <div className="mt-2 space-y-4">
                  {result.experiences.map((exp, index) => (
                    <div key={index}>
                      <div className="flex items-center justify-between gap-3">
                        <div className="flex flex-wrap items-center gap-2">
                          <span className="text-sm font-medium text-slate-800">{exp.name}</span>
                          <Badge color="blue">{experienceTypeLabel(exp.type)}</Badge>
                          {exp.role && <span className="text-xs text-slate-500">{exp.role}</span>}
                          {(exp.start || exp.end) && (
                            <span className="text-xs text-slate-400">
                              {[exp.start, exp.end].filter(Boolean).join(" ~ ")}
                            </span>
                          )}
                        </div>
                        <button type="button" onClick={() => handleRegenerate("experiences", index)} className="of-btn-outline px-2 py-1 text-xs">
                          重生成
                        </button>
                      </div>
                      {(() => {
                        const align = result.jd_alignment?.find((a) => a.exp === index);
                        return align && align.keywords?.length > 0 ? (
                          <div className="mt-1.5 flex flex-wrap items-center gap-1">
                            <span className="text-[11px] text-slate-400">对齐 JD：</span>
                            {align.keywords.map((kw) => (
                              <Badge key={kw} color="green">{kw}</Badge>
                            ))}
                          </div>
                        ) : null;
                      })()}
                      {exp.bullets?.length > 0 && (
                        <ul className="mt-1.5 space-y-1">
                          {exp.bullets.map((bullet, bulletIndex) => (
                            <li key={bulletIndex} className="flex gap-2 text-xs text-slate-600">
                              <span className="text-slate-300">-</span>
                              <span>{bullet}</span>
                            </li>
                          ))}
                        </ul>
                      )}
                    </div>
                  ))}
                </div>
              </section>
            )}

            {result.skills?.length > 0 && (
              <section className="mt-5">
                <div className="flex items-center justify-between">
                  <h3 className="text-sm font-semibold text-slate-800">技能</h3>
                  <button type="button" onClick={() => handleRegenerate("skills")} className="of-btn-outline px-2 py-1 text-xs">
                    重新生成
                  </button>
                </div>
                <div className="mt-2 flex flex-wrap gap-1.5">
                  {result.skills.map((skill) => (
                    <Badge key={skill} color="indigo">{skill}</Badge>
                  ))}
                </div>
              </section>
            )}

            {result.certifications?.length > 0 && (
              <section className="mt-5">
                <div className="flex items-center justify-between">
                  <h3 className="text-sm font-semibold text-slate-800">证书</h3>
                  <button type="button" onClick={() => handleRegenerate("certifications")} className="of-btn-outline px-2 py-1 text-xs">
                    重新生成
                  </button>
                </div>
                <div className="mt-2 flex flex-wrap gap-1.5">
                  {result.certifications.map((cert) => (
                    <Badge key={cert} color="cyan">{cert}</Badge>
                  ))}
                </div>
              </section>
            )}
          </div>

          {result.sections?.length > 0 && (
            <div className="mt-4 text-xs text-slate-400">
              已生成模块：{result.sections.join("、")}
            </div>
          )}

          <div className="mt-5 flex items-center justify-end gap-2 border-t border-slate-200 pt-4">
            <span className="mr-auto text-sm text-slate-500">导出简历：</span>
            <ExportLayoutPicker
              value={exportLayout}
              onChange={(key, columns) => {
                setExportLayout(key);
                setLayoutColumns(columns);
              }}
              className="mr-1"
            />
            <button onClick={() => handleExport("pdf")} className="of-btn-primary px-3 py-1.5 text-xs">
              {Icons.download} 导出 PDF
            </button>
            <button
              onClick={() => handleExport("word")}
              className="of-btn-outline px-3 py-1.5 text-xs disabled:cursor-not-allowed disabled:opacity-50"
              disabled={layoutColumns > 1}
              title={layoutColumns > 1 ? "Word 暂不支持多栏版式，请选择单栏或改用 PDF 导出" : undefined}
            >
              {Icons.download} 导出 Word
            </button>
          </div>

          <div className="mt-3 flex flex-wrap items-center justify-between gap-3 rounded-xl border border-brand-100 bg-brand-50/50 px-4 py-3">
            <span className="text-sm text-slate-600">简历做好了，下一步：</span>
            <div className="flex gap-2">
              <Link to="/job" className="of-btn-outline px-3 py-1.5 text-sm">分析目标岗位</Link>
              <Link to="/job" className="of-btn-primary px-3 py-1.5 text-sm">看岗位匹配度</Link>
            </div>
          </div>
        </Card>
      )}
    </div>
  );
};

export default ResumeGenerate;
