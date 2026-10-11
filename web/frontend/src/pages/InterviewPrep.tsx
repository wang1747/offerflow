import { type ChangeEvent, type FC, useCallback, useEffect, useState } from "react";
import { api } from "../api/client";
import type {
  GeneratedQuestion,
  InterviewArticle,
  InterviewQuestion,
  JDItem,
  ResumeItem,
} from "../types";
import { Card, PageHeader, SectionTitle, Badge, EmptyState, Loading, Tabs } from "../components/ui";
import ResumePicker from "../components/ResumePicker";
import JdPicker from "../components/JdPicker";

type Tab = "library" | "questions" | "simulate";

interface SimulateSummary {
  summary: string;
  total_questions: number;
  questions: string[];
  answers: string[];
  feedbacks: string[];
  status: string;
}

interface RoundHistory {
  question: string;
  answer: string;
  feedback: string;
}

const TABS: { key: Tab; label: string }[] = [
  { key: "library", label: "面经库" },
  { key: "questions", label: "题目生成" },
  { key: "simulate", label: "模拟面试" },
];

const InterviewPrep: FC = () => {
  const [tab, setTab] = useState<Tab>("library");

  const [articles, setArticles] = useState<InterviewArticle[]>([]);
  const [questions, setQuestions] = useState<InterviewQuestion[]>([]);
  const [resumes, setResumes] = useState<ResumeItem[]>([]);
  const [jds, setJds] = useState<JDItem[]>([]);

  const [company, setCompany] = useState("");
  const [position, setPosition] = useState("");
  const [content, setContent] = useState("");
  const [file, setFile] = useState<File | null>(null);
  const [ocrFile, setOcrFile] = useState<File | null>(null);
  const [importing, setImporting] = useState(false);

  const [genResumeId, setGenResumeId] = useState("");
  const [genJdId, setGenJdId] = useState("");
  const [genArticleId, setGenArticleId] = useState("");
  const [generated, setGenerated] = useState<GeneratedQuestion[]>([]);
  const [generating, setGenerating] = useState(false);

  const [simResumeId, setSimResumeId] = useState("");
  const [simJdId, setSimJdId] = useState("");
  const [sessionId, setSessionId] = useState("");
  const [question, setQuestion] = useState("");
  const [answer, setAnswer] = useState("");
  const [history, setHistory] = useState<RoundHistory[]>([]);
  const [summary, setSummary] = useState<SimulateSummary | null>(null);
  const [starting, setStarting] = useState(false);
  const [submitting, setSubmitting] = useState(false);

  const [error, setError] = useState("");
  const [message, setMessage] = useState("");

  const [loadingInit, setLoadingInit] = useState(true);

  const loadAll = useCallback(async () => {
    try {
      const [articlesRes, questionsRes, resumesRes, jdsRes] = await Promise.all([
        api.interview.articles(),
        api.interview.questions(),
        api.resume.list(),
        api.jd.list(),
      ]);
      if (articlesRes.success && articlesRes.data) setArticles(articlesRes.data.items);
      if (questionsRes.success && questionsRes.data) setQuestions(questionsRes.data.items);
      if (resumesRes.success && resumesRes.data) setResumes(resumesRes.data);
      if (jdsRes.success && jdsRes.data) setJds(jdsRes.data);
    } catch (err) {
      setError(err instanceof Error ? err.message : "加载失败");
    } finally {
      setLoadingInit(false);
    }
  }, []);

  useEffect(() => {
    void loadAll();
  }, [loadAll]);

  const handleFile = (e: ChangeEvent<HTMLInputElement>) => {
    setFile(e.target.files?.[0] || null);
  };

  const handleImportText = async () => {
    setError("");
    setMessage("");
    if (!company.trim() || !content.trim()) {
      setError("请填写公司名称和面经内容");
      return;
    }
    setImporting(true);
    try {
      const res = await api.interview.importArticle(
        company.trim(),
        content.trim(),
        position.trim() || undefined,
      );
      if (!res.success) {
        setError(res.error || "导入失败");
        return;
      }
      setMessage(
        res.data?.duplicate
          ? "检测到重复面经，未重复入库"
          : `导入成功，提取 ${res.data?.question_count ?? 0} 道题目`,
      );
      setCompany("");
      setPosition("");
      setContent("");
      await loadAll();
    } catch (err) {
      setError(err instanceof Error ? err.message : "导入失败");
    } finally {
      setImporting(false);
    }
  };

  const handleImportFile = async () => {
    setError("");
    setMessage("");
    if (!company.trim() || !file) {
      setError("请填写公司名称并选择文件");
      return;
    }
    setImporting(true);
    try {
      const res = await api.interview.uploadArticle(company.trim(), file);
      if (!res.success) {
        setError(res.error || "导入失败");
        return;
      }
      setMessage(
        res.data?.duplicate
          ? "检测到重复面经，未重复入库"
          : `导入成功，提取 ${res.data?.question_count ?? 0} 道题目`,
      );
      setCompany("");
      setPosition("");
      setFile(null);
      await loadAll();
    } catch (err) {
      setError(err instanceof Error ? err.message : "导入失败");
    } finally {
      setImporting(false);
    }
  };

  const handleImportOcr = async () => {
    setError("");
    setMessage("");
    if (!company.trim() || !ocrFile) {
      setError("请填写公司名称并选择截图图片");
      return;
    }
    setImporting(true);
    try {
      const res = await api.interview.ocrArticle(
        company.trim(),
        ocrFile,
        position.trim() || undefined,
      );
      if (!res.success) {
        setError(res.error || "OCR 导入失败");
        return;
      }
      setMessage(
        res.data?.duplicate
          ? "检测到重复面经，未重复入库"
          : `OCR 导入成功，提取 ${res.data?.question_count ?? 0} 道题目`,
      );
      setCompany("");
      setPosition("");
      setOcrFile(null);
      await loadAll();
    } catch (err) {
      setError(err instanceof Error ? err.message : "OCR 导入失败");
    } finally {
      setImporting(false);
    }
  };

  const handleDeleteArticle = async (article: InterviewArticle) => {
    if (!window.confirm(`确定删除 ${article.company} 的面经？`)) return;
    setError("");
    setMessage("");
    try {
      await api.interview.delete(article.id);
      setMessage("已删除");
      await loadAll();
    } catch (err) {
      setError(err instanceof Error ? err.message : "删除失败");
    }
  };

  const handleGenerate = async () => {
    setError("");
    setMessage("");
    if (!genResumeId || !genJdId) {
      setError("请选择简历和岗位");
      return;
    }
    setGenerating(true);
    setGenerated([]);
    try {
      const res = await api.interview.generate(genResumeId, genJdId, genArticleId || undefined);
      if (res.success && res.data) {
        setGenerated(res.data.questions);
        setMessage(`已生成 ${res.data.questions.length} 道题目`);
        await loadAll();
      } else {
        setError(res.error || "生成失败");
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : "生成失败");
    } finally {
      setGenerating(false);
    }
  };

  const handleStartSimulate = async () => {
    setError("");
    setMessage("");
    setHistory([]);
    setSummary(null);
    setSessionId("");
    setQuestion("");
    if (!simResumeId || !simJdId) {
      setError("请选择简历和岗位");
      return;
    }
    setStarting(true);
    try {
      const res = await api.interview.simulateStart(simResumeId, simJdId);
      if (res.success && res.data) {
        setSessionId(res.data.session_id);
        setQuestion(res.data.question);
        setAnswer("");
      } else {
        setError(res.error || "无法开始模拟面试");
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : "无法开始模拟面试");
    } finally {
      setStarting(false);
    }
  };

  const handleSubmitAnswer = async () => {
    if (!sessionId || !answer.trim()) return;
    setError("");
    setSubmitting(true);
    try {
      const res = await api.interview.simulateAnswer(sessionId, answer.trim());
      if (!res.success || !res.data) {
        setError(res.error || "提交失败");
        return;
      }
      const data = res.data;
      setHistory((prev) => [...prev, { question, answer: answer.trim(), feedback: data.feedback }]);
      setAnswer("");
      if (data.is_finished) {
        setQuestion("");
        const summaryRes = await api.interview.simulateSummary(sessionId);
        if (summaryRes.success && summaryRes.data) setSummary(summaryRes.data);
      } else if (data.next_question) {
        setQuestion(data.next_question);
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : "提交失败");
    } finally {
      setSubmitting(false);
    }
  };

  const handleFinish = async () => {
    if (!sessionId) return;
    setError("");
    try {
      await api.interview.simulateFinish(sessionId);
      setQuestion("");
      const summaryRes = await api.interview.simulateSummary(sessionId);
      if (summaryRes.success && summaryRes.data) setSummary(summaryRes.data);
    } catch (err) {
      setError(err instanceof Error ? err.message : "结束失败");
    }
  };

  if (loadingInit) return <Loading text="加载面试准备数据..." />;

  return (
    <div className="mx-auto max-w-7xl p-4 sm:p-6">
      <PageHeader
        title="面试准备"
        subtitle="面经库管理、AI 题目生成、模拟面试实战"
        icon={
          <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
            <path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z" />
          </svg>
        }
      />

      {error && (
        <div className="of-alert-error">
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" className="mt-0.5 flex-shrink-0">
            <circle cx="12" cy="12" r="10" />
            <path d="M12 8v4M12 16h.01" />
          </svg>
          {error}
        </div>
      )}
      {message && (
        <div className="of-alert-success">
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" className="mt-0.5 flex-shrink-0">
            <path d="M20 6L9 17l-5-5" />
          </svg>
          {message}
        </div>
      )}

      <Tabs
        tabs={TABS}
        active={tab}
        onChange={(k) => setTab(k as Tab)}
      />

      {/* ===== 面经库 ===== */}
      {tab === "library" && (
        <div className="grid grid-cols-1 gap-6 lg:grid-cols-2">
          <Card className="h-fit p-5">
            <SectionTitle>导入面经</SectionTitle>
            <div className="space-y-3">
              <input
                value={company}
                onChange={(e) => setCompany(e.target.value)}
                placeholder="公司名称"
                className="of-input"
                aria-label="公司名称"
              />
              <input
                value={position}
                onChange={(e) => setPosition(e.target.value)}
                placeholder="职位（可选）"
                className="of-input"
                aria-label="职位"
              />
              <textarea
                value={content}
                onChange={(e) => setContent(e.target.value)}
                placeholder="粘贴面经内容..."
                className="of-textarea h-40 resize-none"
                aria-label="面经内容"
              />
              <div className="space-y-2">
                <label className="flex items-center gap-2 rounded-xl border border-slate-200 bg-slate-50 px-4 py-2.5 text-sm text-slate-600 transition hover:border-brand-300 cursor-pointer">
                  <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
                    <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4" />
                    <path d="M17 8l-5-5-5 5" />
                    <path d="M12 3v12" />
                  </svg>
                  <span>{file ? file.name : "选择面经文件 (PDF/MD/HTML/TXT)"}</span>
                  <input
                    type="file"
                    accept=".pdf,.md,.markdown,.html,.txt"
                    onChange={handleFile}
                    className="hidden"
                    aria-label="面经文件"
                  />
                </label>
                <label className="flex items-center gap-2 rounded-xl border border-slate-200 bg-slate-50 px-4 py-2.5 text-sm text-slate-600 transition hover:border-brand-300 cursor-pointer">
                  <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
                    <path d="M23 19a2 2 0 0 1-2 2H3a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h4l2-3h6l2 3h4a2 2 0 0 1 2 2z" />
                    <circle cx="12" cy="13" r="4" />
                  </svg>
                  <span>{ocrFile ? ocrFile.name : "选择截图 (PNG/JPG/BMP/WebP) — OCR 识别"}</span>
                  <input
                    type="file"
                    accept=".png,.jpg,.jpeg,.bmp,.webp"
                    onChange={(e) => setOcrFile(e.target.files?.[0] || null)}
                    className="hidden"
                    aria-label="面经截图"
                  />
                </label>
              </div>
              <div className="flex flex-wrap gap-2 pt-1">
                <button onClick={handleImportText} disabled={importing} className="of-btn-primary">
                  {importing ? "导入中..." : "导入文本"}
                </button>
                <button onClick={handleImportFile} disabled={importing} className="of-btn-outline">
                  上传文件
                </button>
                <button onClick={handleImportOcr} disabled={importing || !ocrFile} className="of-btn-outline">
                  OCR 导入截图
                </button>
              </div>
            </div>
          </Card>

          <Card className="p-5">
            <SectionTitle count={articles.length}>面经列表</SectionTitle>
            {articles.length === 0 ? (
              <EmptyState title="暂无面经" description="导入面经后，AI 将自动提取面试题目" />
            ) : (
              <div className="space-y-2">
                {articles.map((article) => (
                  <details key={article.id} className="group rounded-xl border border-slate-200 transition hover:border-slate-300">
                    <summary className="flex cursor-pointer list-none items-center justify-between gap-3 p-3">
                      <div className="min-w-0">
                        <div className="flex items-center gap-2 text-sm font-medium text-slate-900">
                          {article.company}
                          {article.position && <span className="text-slate-400">· {article.position}</span>}
                        </div>
                        <div className="mt-1 flex items-center gap-2">
                          {article.difficulty && <Badge color="amber">{article.difficulty}</Badge>}
                          {article.tags?.slice(0, 3).map((tag, i) => (
                            <Badge key={i} color="blue">{tag}</Badge>
                          ))}
                        </div>
                      </div>
                      <button
                        onClick={(e) => {
                          e.preventDefault();
                          e.stopPropagation();
                          handleDeleteArticle(article);
                        }}
                        className="text-xs text-rose-500 transition hover:text-rose-700 flex-shrink-0"
                        aria-label={`删除 ${article.company} 面经`}
                      >
                        删除
                      </button>
                    </summary>
                    <div className="border-t border-slate-100 px-3 py-3 text-sm">
                      {article.questions?.length ? (
                        <ul className="mb-3 space-y-1">
                          {article.questions.map((q, idx) => (
                            <li key={idx} className="flex items-start gap-2 text-slate-700">
                              <span className="mt-1.5 h-1 w-1 flex-shrink-0 rounded-full bg-brand-400" />
                              {q}
                            </li>
                          ))}
                        </ul>
                      ) : (
                        <p className="mb-3 text-slate-400">未提取到题目</p>
                      )}
                      <p className="line-clamp-4 whitespace-pre-wrap text-slate-500">{article.raw_content}</p>
                    </div>
                  </details>
                ))}
              </div>
            )}
          </Card>
        </div>
      )}

      {/* ===== 题目生成 ===== */}
      {tab === "questions" && (
        <div className="space-y-6">
          <Card className="p-5">
            <SectionTitle>基于简历 + JD 生成题目</SectionTitle>
            <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
              <ResumePicker
                value={genResumeId}
                onChange={setGenResumeId}
                resumes={resumes}
                onRefresh={loadAll}
                label="选择简历"
                emptyHint="还没有简历 —— 上传一份就能生成针对性题目，其他模块也会自动看到它"
              />
              <JdPicker
                value={genJdId}
                onChange={setGenJdId}
                jds={jds}
                onRefresh={loadAll}
                label="选择 JD"
              />
            </div>
            <div className="mt-4 flex flex-col gap-3 sm:flex-row">
              <select value={genArticleId} onChange={(e) => setGenArticleId(e.target.value)} className="of-select sm:flex-1" aria-label="选择面经">
                <option value="">结合面经（可选）</option>
                {articles.map((article) => (
                  <option key={article.id} value={article.id}>{article.company}{article.position ? ` · ${article.position}` : ""}</option>
                ))}
              </select>
              <button
                onClick={handleGenerate}
                disabled={generating || !genResumeId || !genJdId}
                className="of-btn-primary sm:w-40"
              >
                {generating ? (
                  <>
                    <div className="h-4 w-4 animate-spin rounded-full border-2 border-white/30 border-t-white" />
                    生成中...
                  </>
                ) : (
                  <>
                    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
                      <path d="M12 3l1.5 5L19 9.5 13.5 11 12 16l-1.5-5L5 9.5 10.5 8z" />
                    </svg>
                    生成题目
                  </>
                )}
              </button>
            </div>
            {(!genResumeId || !genJdId) && (
              <p className="mt-2 text-xs text-slate-400">
                需要一份简历和一个岗位。没有的话就在上面直接上传 / 粘贴，不必先去别的页面准备。
              </p>
            )}
          </Card>

          {generated.length > 0 && (
            <Card className="border-emerald-200 p-5">
              <SectionTitle count={generated.length}>
                本次生成
              </SectionTitle>
              <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
                {generated.map((item, idx) => (
                  <details key={idx} className="rounded-xl border border-slate-100 bg-slate-50/50 p-4">
                    <summary className="flex cursor-pointer list-none items-start justify-between gap-3">
                      <p className="text-sm font-medium text-slate-900">{item.question}</p>
                      <div className="flex flex-shrink-0 gap-1">
                        {item.category && <Badge color="indigo">{item.category}</Badge>}
                        {item.difficulty && <Badge color="amber">{item.difficulty}</Badge>}
                      </div>
                    </summary>
                    {item.answer && (
                      <div className="mt-3 rounded-lg bg-white p-3">
                        <div className="flex items-center justify-between">
                          <span className="text-xs font-medium text-emerald-600">参考答案</span>
                          <span className="text-xs text-slate-400">AI 生成，请核实事实</span>
                        </div>
                        {item.suspicious_numbers && item.suspicious_numbers.length > 0 && (
                          <div className="mt-2 flex items-start gap-1.5 rounded-lg bg-amber-50 px-3 py-2 text-xs text-amber-700">
                            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" className="mt-0.5 flex-shrink-0">
                              <path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z" />
                              <path d="M12 9v4M12 17h.01" />
                            </svg>
                            <span>以下数字未在你的简历中出现，请核对是否符合实际：<b>{item.suspicious_numbers.join("、")}</b></span>
                          </div>
                        )}
                        <textarea
                          className="mt-2 min-h-[80px] w-full resize-y rounded-lg border border-slate-200 bg-slate-50/50 p-2.5 text-sm leading-relaxed text-slate-700 outline-none transition focus:border-emerald-400"
                          defaultValue={item.answer}
                        />
                      </div>
                    )}
                  </details>
                ))}
              </div>
            </Card>
          )}

          <Card className="p-5">
            <SectionTitle count={questions.length}>面试题库</SectionTitle>
            {questions.length === 0 ? (
              <EmptyState title="暂无题目" description="生成题目后，题库会自动收录" />
            ) : (
              <div className="grid grid-cols-1 gap-2 sm:grid-cols-2">
                {questions.map((item) => (
                  <details key={item.id} className="rounded-xl bg-slate-50/50 p-3 text-sm">
                    <summary className="flex cursor-pointer list-none items-start justify-between gap-4">
                      <div className="min-w-0 flex-1">
                        <p className="font-medium text-slate-900">{item.question}</p>
                        {(item.category || item.difficulty) && (
                          <div className="mt-1.5 flex gap-1">
                            {item.category && <Badge color="blue">{item.category}</Badge>}
                            {item.difficulty && <Badge color="amber">{item.difficulty}</Badge>}
                          </div>
                        )}
                      </div>
                    </summary>
                    {item.answer && (
                      <div className="mt-3 rounded-lg bg-white p-3">
                        <div className="flex items-center justify-between">
                          <span className="text-xs font-medium text-emerald-600">参考答案</span>
                          <span className="text-xs text-slate-400">AI 生成，请核实事实</span>
                        </div>
                        <textarea
                          className="mt-2 min-h-[80px] w-full resize-y rounded-lg border border-slate-200 bg-slate-50/50 p-2.5 text-sm leading-relaxed text-slate-700 outline-none transition focus:border-emerald-400"
                          defaultValue={item.answer}
                        />
                      </div>
                    )}
                  </details>
                ))}
              </div>
            )}
          </Card>
        </div>
      )}

      {/* ===== 模拟面试 ===== */}
      {tab === "simulate" && (
        <div className="grid grid-cols-1 gap-6 lg:grid-cols-3">
          <Card className="h-fit p-5">
            <SectionTitle>模拟面试</SectionTitle>
            <div className="space-y-3">
              <ResumePicker
                value={simResumeId}
                onChange={setSimResumeId}
                resumes={resumes}
                onRefresh={loadAll}
                label="选择简历"
                disabled={!!sessionId}
                emptyHint="还没有简历 —— 上传一份就能开始模拟面试，其他模块也会自动看到它"
              />
              <JdPicker
                value={simJdId}
                onChange={setSimJdId}
                jds={jds}
                onRefresh={loadAll}
                label="选择 JD"
                disabled={!!sessionId}
              />
              <button
                onClick={handleStartSimulate}
                disabled={starting || !!sessionId || !simResumeId || !simJdId}
                className="of-btn-primary w-full"
              >
                {starting ? (
                  <>
                    <div className="h-4 w-4 animate-spin rounded-full border-2 border-white/30 border-t-white" />
                    准备中...
                  </>
                ) : (
                  <>
                    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
                      <path d="M4.5 16.5c-1.5 1.26-2 5-2 5s3.74-.5 5-2c.71-.84.7-2.13-.09-2.91a2.18 2.18 0 0 0-3.16-.1z" />
                      <path d="M12 15l-3-3a22 22 0 0 1 2-3.95A12.88 12.88 0 0 1 22 2c0 2.72-.78 7.5-6 11a22.35 22.35 0 0 1-4 2z" />
                      <path d="M9 12h.01M15 18h.01" />
                    </svg>
                    开始面试
                  </>
                )}
              </button>
              {(!simResumeId || !simJdId) && (
                <p className="text-xs leading-relaxed text-slate-400">
                  需要一份简历和一个岗位。没有的话就在上面直接上传 / 粘贴，不必先去别的页面准备。
                </p>
              )}
              {history.length > 0 && (
                <div className="mt-3 rounded-xl bg-slate-50 p-3 text-center">
                  <span className="text-2xl font-bold text-brand-600">{history.length}</span>
                  <span className="text-sm text-slate-500"> 轮已答</span>
                </div>
              )}
            </div>
          </Card>

          <Card className="p-5 lg:col-span-2">
            {!sessionId && !summary ? (
              <EmptyState
                icon={
                  <svg width="48" height="48" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
                    <path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z" />
                  </svg>
                }
                title="选择简历和岗位后开始模拟面试"
                description="AI 将基于你的简历和岗位要求进行多轮提问与评估"
              />
            ) : summary ? (
              <div>
                <div className="mb-4 flex items-center gap-2">
                  <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" className="text-emerald-600">
                    <path d="M20 6L9 17l-5-5" />
                  </svg>
                  <h2 className="of-section-title text-emerald-700">面试总结</h2>
                </div>
                <div className="mb-4 rounded-xl bg-emerald-50 p-4">
                  <p className="text-sm leading-relaxed text-slate-700 whitespace-pre-wrap">{summary.summary}</p>
                </div>
                <div className="flex items-center gap-2">
                  <Badge color="green">共 {summary.total_questions} 轮</Badge>
                  <Badge color="blue">{summary.status}</Badge>
                </div>
              </div>
            ) : (
              <div className="space-y-4">
                <div className="rounded-xl border border-brand-200 bg-brand-50 p-4">
                  <div className="mb-1.5 flex items-center gap-1.5">
                    <div className="h-2 w-2 animate-pulse rounded-full bg-brand-500" />
                    <span className="text-xs font-medium text-brand-600">面试官提问</span>
                  </div>
                  <p className="text-sm font-medium text-slate-800">{question}</p>
                </div>
                <textarea
                  value={answer}
                  onChange={(e) => setAnswer(e.target.value)}
                  placeholder="输入你的回答..."
                  className="of-textarea h-32 resize-none"
                  aria-label="回答"
                />
                <div className="flex flex-wrap gap-2">
                  <button onClick={handleSubmitAnswer} disabled={submitting || !answer.trim()} className="of-btn-primary">
                    {submitting ? (
                      <>
                        <div className="h-4 w-4 animate-spin rounded-full border-2 border-white/30 border-t-white" />
                        分析中...
                      </>
                    ) : (
                      "提交回答"
                    )}
                  </button>
                  <button onClick={handleFinish} className="of-btn-outline" title="提前结束并查看总结">
                    结束面试
                  </button>
                </div>

                {history.length > 0 && (
                  <div className="space-y-3 pt-2">
                    <p className="text-xs font-medium uppercase tracking-wide text-slate-400">历史记录</p>
                    {history.map((round, idx) => (
                      <div key={idx} className="rounded-xl border border-slate-100 bg-slate-50/50 p-4 text-sm">
                        <div className="mb-2 flex items-start gap-2">
                          <Badge color="indigo">Q{idx + 1}</Badge>
                          <p className="font-medium text-slate-900">{round.question}</p>
                        </div>
                        <div className="mb-2 ml-1 border-l-2 border-slate-200 pl-3 text-slate-600">
                          <span className="text-xs text-slate-400">回答：</span>{round.answer}
                        </div>
                        <div className="ml-1 border-l-2 border-emerald-300 pl-3 text-emerald-700">
                          <span className="text-xs text-emerald-400">反馈：</span>{round.feedback}
                        </div>
                      </div>
                    ))}
                  </div>
                )}
              </div>
            )}
          </Card>
        </div>
      )}
    </div>
  );
};

export default InterviewPrep;
