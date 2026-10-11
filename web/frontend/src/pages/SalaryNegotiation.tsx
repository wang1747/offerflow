import { type FC, useCallback, useEffect, useState } from "react";
import { api } from "../api/client";
import type { NegotiationSummary, ResumeItem, SalaryReference, SalaryBenchmarkInfo } from "../types";
import { Card, PageHeader, SectionTitle, Badge, EmptyState, Tabs, Modal } from "../components/ui";
import ResumePicker from "../components/ResumePicker";

const SCENARIOS: { key: string; label: string; desc: string }[] = [
  { key: "offer", label: "初次谈薪", desc: "刚收到 offer，第一次谈薪资" },
  { key: "counter", label: "应对压价", desc: "HR 以预算/市场行情压你的价" },
  { key: "raise", label: "争取涨幅", desc: "内部调薪或升职谈判" },
  { key: "final", label: "应对最终报价", desc: "HR 声称「不能再加」" },
];

const CITY_OPTIONS = [
  "北京", "上海", "深圳", "广州",
  "杭州", "成都", "南京", "武汉", "苏州", "西安", "重庆", "天津",
  "长沙", "郑州", "青岛", "合肥", "其他",
];

const moneyIcon = (
  <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
    <rect x="2" y="6" width="20" height="12" rx="2" />
    <circle cx="12" cy="12" r="2.5" />
    <path d="M6 12h.01M18 12h.01" />
  </svg>
);

interface Round {
  hr: string;
  answer: string;
  coaching: string;
}

const SalaryNegotiation: FC = () => {
  // ===== 薪资参考（估薪）state =====
  const [activeTab, setActiveTab] = useState("reference");
  const [resumes, setResumes] = useState<ResumeItem[]>([]);
  const [selectedResumeId, setSelectedResumeId] = useState("");
  const [city, setCity] = useState("");
  const [positionHint, setPositionHint] = useState("");
  const [reference, setReference] = useState<SalaryReference | null>(null);
  const [estimating, setEstimating] = useState(false);
  const [referenceError, setReferenceError] = useState("");
  const [benchmarkInfo, setBenchmarkInfo] = useState<SalaryBenchmarkInfo | null>(null);
  const [benchmarkRefreshing, setBenchmarkRefreshing] = useState(false);
  const [benchmarkMsg, setBenchmarkMsg] = useState("");
  const [importOpen, setImportOpen] = useState(false);
  const [importJson, setImportJson] = useState("");
  const [importYear, setImportYear] = useState("");
  const [importSource, setImportSource] = useState("");
  const [importing, setImporting] = useState(false);
  const [importMsg, setImportMsg] = useState("");

  // ===== 谈判演练 state =====
  const [scenario, setScenario] = useState("offer");
  const [targetSalary, setTargetSalary] = useState("");
  const [bottomSalary, setBottomSalary] = useState("");
  const [context, setContext] = useState("");
  const [sessionId, setSessionId] = useState("");
  const [hrMessage, setHrMessage] = useState("");
  const [answer, setAnswer] = useState("");
  const [history, setHistory] = useState<Round[]>([]);
  const [summary, setSummary] = useState<NegotiationSummary | null>(null);
  const [starting, setStarting] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState("");

  const loadResumes = useCallback(async () => {
    try {
      const res = await api.resume.list();
      if (!res.success || !res.data) return;
      const list = res.data;
      setResumes(list);
      // 只有一份时自动选中，省一次点击；多于一份让用户自己挑
      if (list.length === 1) {
        setSelectedResumeId((prev) => prev || list[0].id);
      }
    } catch {
      // 后端不可达 / 离线时静默：页面其他部分仍可用
    }
  }, []);

  useEffect(() => {
    let alive = true;
    void loadResumes();
    api.salaryNegotiation.benchmarkInfo()
      .then((res) => {
        if (alive && res.success && res.data) setBenchmarkInfo(res.data);
      })
      .catch(() => {});
    return () => {
      alive = false;
    };
  }, [loadResumes]);

  const handleRefreshBenchmark = async () => {
    setBenchmarkMsg("");
    setBenchmarkRefreshing(true);
    try {
      const res = await api.salaryNegotiation.benchmarkRefresh();
      if (res.success && res.data) {
        setBenchmarkInfo(res.data);
        setBenchmarkMsg(`已刷新到 v${res.data.version}（${res.data.data_year}）`);
        setReference(null);
      } else {
        setBenchmarkMsg(res.error || "刷新失败");
      }
    } catch (err) {
      setBenchmarkMsg(err instanceof Error ? err.message : "刷新失败");
    } finally {
      setBenchmarkRefreshing(false);
    }
  };

  const handleImport = async () => {
    setImportMsg("");
    let parsed: Record<string, unknown>;
    try {
      parsed = JSON.parse(importJson);
    } catch {
      setImportMsg("JSON 格式不正确，请检查");
      return;
    }
    if (!parsed.degree_base || !parsed.direction_coef || !parsed.city_tiers) {
      setImportMsg("缺少必要字段：degree_base / direction_coef / city_tiers");
      return;
    }
    setImporting(true);
    try {
      const res = await api.salaryNegotiation.benchmarkImport({
        data: parsed,
        data_year: importYear.trim() || undefined,
        source: importSource.trim() || undefined,
      });
      if (res.success && res.data) {
        setBenchmarkInfo(res.data);
        setImportOpen(false);
        setImportJson("");
        setImportYear("");
        setImportSource("");
        setBenchmarkMsg(`已导入到 v${res.data.version}（${res.data.data_year}）`);
        setReference(null);
      } else {
        setImportMsg(res.error || "导入失败");
      }
    } catch (err) {
      setImportMsg(err instanceof Error ? err.message : "导入失败");
    } finally {
      setImporting(false);
    }
  };

  const handleEstimate = async () => {
    setReferenceError("");
    if (!selectedResumeId) {
      setReferenceError("请先选择一份简历（没有的话直接上传一份）");
      return;
    }
    setEstimating(true);
    setReference(null);
    try {
      const res = await api.salaryNegotiation.reference({
        resume_id: selectedResumeId,
        target_city: city && city !== "其他" ? city : undefined,
        position_hint: positionHint.trim() || undefined,
      });
      if (res.success && res.data) {
        setReference(res.data);
      } else {
        setReferenceError(res.error || "估薪失败，请稍后重试");
      }
    } catch (err) {
      setReferenceError(err instanceof Error ? err.message : "估薪失败，请稍后重试");
    } finally {
      setEstimating(false);
    }
  };

  const handleUseForNegotiation = () => {
    if (!reference) return;
    setTargetSalary(`${reference.suggest_ask} 元 / 月`);
    setBottomSalary(`${reference.suggest_bottom} 元 / 月`);
    setContext(`市场参考价约 ${reference.salary_range.low}~${reference.salary_range.high} 元/月（${reference.direction}），期望落地 ${reference.suggest_target} 元`);
    setActiveTab("practice");
  };

  const handleStart = async () => {
    setError("");
    setHistory([]);
    setSummary(null);
    setSessionId("");
    setHrMessage("");
    setAnswer("");
    if (!targetSalary.trim()) {
      setError("请先填写开口价（这是你打算报给 HR 的数，演练的核心）");
      return;
    }
    setStarting(true);
    try {
      const res = await api.salaryNegotiation.start({
        scenario,
        target_salary: targetSalary.trim() || undefined,
        bottom_salary: bottomSalary.trim() || undefined,
        context: context.trim() || undefined,
      });
      if (res.success && res.data) {
        setSessionId(res.data.session_id);
        setHrMessage(res.data.hr_message);
      } else {
        setError(res.error || "无法开始谈判");
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : "无法开始谈判");
    } finally {
      setStarting(false);
    }
  };

  const handleSubmit = async () => {
    if (!sessionId || !answer.trim()) return;
    setError("");
    setSubmitting(true);
    try {
      const res = await api.salaryNegotiation.answer(sessionId, answer.trim());
      if (!res.success || !res.data) {
        setError(res.error || "提交失败");
        return;
      }
      const data = res.data;
      setHistory((prev) => [...prev, { hr: hrMessage, answer: answer.trim(), coaching: data.coaching }]);
      setAnswer("");
      if (data.is_finished) {
        setHrMessage("");
        const sumRes = await api.salaryNegotiation.summary(sessionId);
        if (sumRes.success && sumRes.data) setSummary(sumRes.data);
      } else if (data.next_hr_message) {
        setHrMessage(data.next_hr_message);
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : "提交失败");
    } finally {
      setSubmitting(false);
    }
  };

  const handleReset = () => {
    setSessionId("");
    setHrMessage("");
    setAnswer("");
    setHistory([]);
    setSummary(null);
    setError("");
  };

  const fmt = (n: number) => `¥${n.toLocaleString("zh-CN")}`;

  return (
    <div className="mx-auto max-w-7xl p-4 sm:p-6">
      <PageHeader
        title="薪资"
        subtitle="先测测你的市场价，再和 AI 扮演的 HR 实战演练谈薪"
        icon={moneyIcon}
      />

      <Tabs
        active={activeTab}
        onChange={setActiveTab}
        tabs={[
          { key: "reference", label: "薪资参考" },
          { key: "practice", label: "谈判演练" },
        ]}
      />

      {/* ===================== 薪资参考 ===================== */}
      {activeTab === "reference" && (
        <div className="grid grid-cols-1 gap-6 lg:grid-cols-3">
          <Card className="h-fit p-5">
            <SectionTitle>告诉我你的情况</SectionTitle>
            <div className="space-y-3">
              <ResumePicker
                value={selectedResumeId}
                onChange={setSelectedResumeId}
                resumes={resumes}
                onRefresh={loadResumes}
                label="选择你的简历"
                emptyHint="还没有简历 —— 上传一份就能估市场价（只读取简历内容，不会改动它）"
              />

              <div>
                <label className="mb-1.5 block text-xs font-medium text-slate-500">意向城市（选填）</label>
                <select className="of-select w-full" value={city} onChange={(e) => setCity(e.target.value)}>
                  <option value="">默认一线城市</option>
                  {CITY_OPTIONS.map((c) => (
                    <option key={c} value={c}>{c}</option>
                  ))}
                </select>
              </div>

              <div>
                <label className="mb-1.5 block text-xs font-medium text-slate-500">意向岗位（选填，帮你判断方向）</label>
                <input
                  className="of-input"
                  placeholder="如：后端开发 / 数据分析 / 小学教师"
                  value={positionHint}
                  onChange={(e) => setPositionHint(e.target.value)}
                />
              </div>
            </div>

            {referenceError && (
              <div className="of-alert-error mt-3">
                <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" className="mt-0.5 flex-shrink-0">
                  <circle cx="12" cy="12" r="10" />
                  <path d="M12 8v4M12 16h.01" />
                </svg>
                {referenceError}
              </div>
            )}

            <button
              onClick={handleEstimate}
              disabled={estimating || !selectedResumeId}
              className="of-btn-primary mt-4 w-full"
            >
              {estimating ? (
                <>
                  <div className="h-4 w-4 animate-spin rounded-full border-2 border-white/30 border-t-white" />
                  估算中...
                </>
              ) : (
                "帮我估个市场价"
              )}
            </button>
            {!selectedResumeId && (
              <p className="mt-2 text-xs leading-relaxed text-slate-400">
                先选一份简历（没有的话就在上面直接上传一份），才能估算市场价。
              </p>
            )}

            <div className="mt-3 border-t border-slate-100 pt-3">
              <div className="flex items-center justify-between">
                <p className="text-[11px] text-slate-400">
                  市场数据 {benchmarkInfo ? `v${benchmarkInfo.version} · ${benchmarkInfo.data_year} 届` : "加载中"}
                </p>
                <div className="flex items-center gap-3">
                  <button
                    onClick={() => { setImportMsg(""); setImportOpen(true); }}
                    className="text-[11px] font-medium text-slate-500 hover:text-slate-700"
                  >
                    导入权威数据
                  </button>
                  <button
                    onClick={handleRefreshBenchmark}
                    disabled={benchmarkRefreshing}
                    className="text-[11px] font-medium text-brand-600 hover:text-brand-700 disabled:text-slate-300"
                  >
                    {benchmarkRefreshing ? "刷新中..." : "刷新市场数据"}
                  </button>
                </div>
              </div>
              {benchmarkMsg && <p className="mt-1 text-[11px] text-slate-500">{benchmarkMsg}</p>}
            </div>
          </Card>

          <Card className="p-5 lg:col-span-2">
            {!reference ? (
              <EmptyState
                icon={moneyIcon}
                title="还不知道该要多少钱？"
                description="选好简历，点「帮我估个市场价」，我会参考 2026 届应届生市场行情，给你一个合理区间和谈判建议"
              />
            ) : (
              <div className="space-y-5">
                {/* 价格卡片 */}
                <div className="rounded-2xl bg-gradient-to-br from-brand-500 to-brand-600 p-5 text-white">
                  <div className="flex flex-wrap items-end justify-between gap-3">
                    <div>
                      <p className="text-xs font-medium text-white/80">建议开口价（往高了报，留压价空间）</p>
                      <p className="mt-1 text-4xl font-bold">{fmt(reference.suggest_ask)}<span className="ml-1 text-base font-normal text-white/70">/ 月</span></p>
                    </div>
                    <div className="text-right">
                      <p className="text-xs font-medium text-white/80">合理区间</p>
                      <p className="mt-1 text-lg font-bold">{fmt(reference.salary_range.low)} ~ {fmt(reference.salary_range.high)}</p>
                      <p className="text-xs text-white/70">目标 {fmt(reference.suggest_target)} · 底线 {fmt(reference.suggest_bottom)}</p>
                    </div>
                  </div>
                </div>

                {/* 画像标签 */}
                <div className="flex flex-wrap gap-2">
                  <Badge color="blue">{reference.degree}</Badge>
                  <Badge color="indigo">{reference.school_tier}</Badge>
                  <Badge color="cyan">{reference.direction}</Badge>
                  <Badge color="green">{reference.city}</Badge>
                </div>

                {/* 解读文案 */}
                {reference.analysis && (
                  <div className="rounded-xl border border-slate-100 bg-slate-50/60 p-4">
                    <p className="whitespace-pre-wrap text-sm leading-relaxed text-slate-700">{reference.analysis}</p>
                  </div>
                )}

                {/* 市场依据 */}
                <div className="rounded-xl border border-brand-100 bg-brand-50/50 p-4">
                  <p className="text-xs font-medium text-brand-600">市场依据</p>
                  <p className="mt-1 text-xs leading-relaxed text-slate-600">{reference.market_basis}</p>
                </div>

                {/* 数据来源 */}
                {reference.sources?.length > 0 && (
                  <div className="rounded-xl border border-slate-100 bg-slate-50/50 p-4">
                    <div className="flex items-center justify-between">
                      <p className="text-xs font-medium text-slate-500">数据来源</p>
                      <p className="text-[11px] text-slate-400">
                        数据版本 v{reference.data_version} · {reference.data_year} 届行情
                      </p>
                    </div>
                    <ul className="mt-1.5 space-y-1">
                      {reference.sources.map((s, i) => (
                        <li key={i} className="flex items-start gap-2 text-xs text-slate-500">
                          <span className="mt-0.5 text-slate-300">•</span>
                          {s}
                        </li>
                      ))}
                    </ul>
                  </div>
                )}

                {/* 注意事项 */}
                {reference.caveats.length > 0 && (
                  <div className="space-y-1">
                    {reference.caveats.map((c, i) => (
                      <p key={i} className="flex items-start gap-2 text-xs text-slate-400">
                        <span className="mt-0.5 text-slate-300">•</span>
                        {c}
                      </p>
                    ))}
                  </div>
                )}

                <button onClick={handleUseForNegotiation} className="of-btn-primary w-full">
                  用这个参考价去演练谈判
                </button>
              </div>
            )}
          </Card>
        </div>
      )}

      {/* ===================== 谈判演练 ===================== */}
      {activeTab === "practice" && (
        <div>
          {error && (
            <div className="of-alert-error mb-4">
              <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" className="mt-0.5 flex-shrink-0">
                <circle cx="12" cy="12" r="10" />
                <path d="M12 8v4M12 16h.01" />
              </svg>
              {error}
            </div>
          )}

          <div className="grid grid-cols-1 gap-6 lg:grid-cols-3">
            <Card className="h-fit p-5">
              <SectionTitle>谈判设定</SectionTitle>

              <div className="mb-2 text-xs font-medium text-slate-500">选择场景</div>
              <div className="space-y-2">
                {SCENARIOS.map((s) => (
                  <button
                    key={s.key}
                    type="button"
                    onClick={() => setScenario(s.key)}
                    className={`w-full rounded-xl border p-3 text-left transition ${
                      scenario === s.key
                        ? "border-brand-400 bg-brand-50/60 ring-1 ring-brand-300"
                        : "border-slate-200 bg-white hover:border-slate-300"
                    }`}
                  >
                    <div className="text-sm font-medium text-slate-900">{s.label}</div>
                    <div className="mt-0.5 text-xs text-slate-500">{s.desc}</div>
                  </button>
                ))}
              </div>

              <div className="mt-4 space-y-3">
                <div>
                  <label className="mb-1.5 block text-xs font-medium text-slate-500">开口价（你要报的数，必填）</label>
                  <input
                    className="of-input"
                    placeholder="如：14k / 15k·14薪"
                    value={targetSalary}
                    onChange={(e) => setTargetSalary(e.target.value)}
                  />
                </div>
                <div>
                  <label className="mb-1.5 block text-xs font-medium text-slate-500">底线薪资（低于它就不谈）</label>
                  <input
                    className="of-input"
                    placeholder="如：13k"
                    value={bottomSalary}
                    onChange={(e) => setBottomSalary(e.target.value)}
                  />
                </div>
                <div>
                  <label className="mb-1.5 block text-xs font-medium text-slate-500">业绩证明点（你的谈判筹码）</label>
                  <textarea
                    className="of-textarea h-24 resize-none"
                    placeholder="例：主导 XX 项目，性能提升 80%；有 2 年相关经验；带过 3 人团队"
                    value={context}
                    onChange={(e) => setContext(e.target.value)}
                  />
                </div>
              </div>

              {sessionId || summary ? (
                <button onClick={handleReset} className="of-btn-outline mt-4 w-full">
                  重新演练
                </button>
              ) : (
                <button onClick={handleStart} disabled={starting} className="of-btn-primary mt-4 w-full">
                  {starting ? (
                    <>
                      <div className="h-4 w-4 animate-spin rounded-full border-2 border-white/30 border-t-white" />
                      准备中...
                    </>
                  ) : (
                    "开始演练"
                  )}
                </button>
              )}

              {history.length > 0 && (
                <div className="mt-3 rounded-xl bg-slate-50 p-3 text-center">
                  <span className="text-2xl font-bold text-brand-600">{history.length}</span>
                  <span className="text-sm text-slate-500"> / 5 轮已谈</span>
                </div>
              )}
            </Card>

            <Card className="p-5 lg:col-span-2">
              {!sessionId && !summary ? (
                <EmptyState
                  icon={moneyIcon}
                  title="填好谈判设定后开始演练"
                  description="AI 会扮演 HR，用真实的压价话术和你谈判，每轮给你教练点评。还不确定要多少？去「薪资参考」先估个价"
                />
              ) : summary ? (
                <div>
                  <div className="mb-4 flex items-center gap-2">
                    <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" className="text-emerald-600">
                      <path d="M20 6L9 17l-5-5" />
                    </svg>
                    <h2 className="of-section-title text-emerald-700">谈判总结</h2>
                  </div>
                  <div className="mb-4 rounded-xl bg-emerald-50 p-4">
                    <p className="whitespace-pre-wrap text-sm leading-relaxed text-slate-700">{summary.summary}</p>
                  </div>
                  <div className="flex items-center gap-2">
                    <Badge color="green">共 {summary.round_count} 轮</Badge>
                    <Badge color="blue">{summary.status === "finished" ? "已结束" : "进行中"}</Badge>
                  </div>
                  {history.length > 0 && (
                    <div className="mt-4 space-y-3">
                      <p className="text-xs font-medium uppercase tracking-wide text-slate-400">逐轮教练点评</p>
                      {history.map((round, idx) => (
                        <div key={idx} className="rounded-xl border border-slate-100 bg-slate-50/50 p-4 text-sm">
                          <div className="mb-1 flex items-center gap-2">
                            <Badge color="indigo">第 {idx + 1} 轮</Badge>
                            <span className="text-xs text-slate-400">HR：{round.hr}</span>
                          </div>
                          <div className="mb-1 text-slate-600">你：{round.answer}</div>
                          <div className="border-l-2 border-emerald-300 pl-3 text-emerald-700">{round.coaching}</div>
                        </div>
                      ))}
                    </div>
                  )}
                </div>
              ) : (
                <div className="space-y-4">
                  <div className="rounded-xl border border-brand-200 bg-brand-50 p-4">
                    <div className="mb-1.5 flex items-center gap-1.5">
                      <div className="h-2 w-2 animate-pulse rounded-full bg-brand-500" />
                      <span className="text-xs font-medium text-brand-600">HR（招聘方）</span>
                    </div>
                    <p className="text-sm font-medium text-slate-800">{hrMessage}</p>
                  </div>

                  <textarea
                    value={answer}
                    onChange={(e) => setAnswer(e.target.value)}
                    placeholder="输入你的回应..."
                    className="of-textarea h-24 resize-none"
                  />
                  <button onClick={handleSubmit} disabled={submitting || !answer.trim()} className="of-btn-primary">
                    {submitting ? (
                      <>
                        <div className="h-4 w-4 animate-spin rounded-full border-2 border-white/30 border-t-white" />
                        分析中...
                      </>
                    ) : (
                      "回应 HR"
                    )}
                  </button>

                  {history.length > 0 && (
                    <div className="space-y-3 pt-2">
                      <p className="text-xs font-medium uppercase tracking-wide text-slate-400">历史记录 · 教练点评</p>
                      {history.map((round, idx) => (
                        <div key={idx} className="rounded-xl border border-slate-100 bg-slate-50/50 p-4 text-sm">
                          <div className="mb-1.5 flex items-start gap-2">
                            <Badge color="indigo">第 {idx + 1} 轮</Badge>
                            <div className="min-w-0">
                              <p className="text-xs text-slate-400">HR：{round.hr}</p>
                              <p className="mt-0.5 font-medium text-slate-900">{round.answer}</p>
                            </div>
                          </div>
                          <div className="border-l-2 border-emerald-300 pl-3">
                            <span className="text-xs font-medium text-emerald-500">教练点评：</span>
                            <span className="text-emerald-700">{round.coaching}</span>
                          </div>
                        </div>
                      ))}
                    </div>
                  )}
                </div>
              )}
            </Card>
          </div>
        </div>
      )}

      <Modal
        open={importOpen}
        onClose={() => setImportOpen(false)}
        title="导入权威市场薪资数据"
        footer={
          <>
            <button onClick={() => setImportOpen(false)} className="of-btn-outline">
              取消
            </button>
            <button onClick={handleImport} disabled={importing} className="of-btn-primary">
              {importing ? "导入中..." : "导入"}
            </button>
          </>
        }
      >
        <div className="space-y-3">
          <p className="text-xs leading-relaxed text-slate-500">
            拿到权威薪酬报告后，把整理好的基准数据（JSON）粘贴进来，会作为新版本覆盖当前数据。结构需包含
            <code className="mx-1 rounded bg-slate-100 px-1">degree_base</code>
            <code className="mx-1 rounded bg-slate-100 px-1">direction_coef</code>
            <code className="rounded bg-slate-100 px-1">city_tiers</code>
            三个字段。
          </p>
          <div className="grid grid-cols-2 gap-3">
            <div>
              <label className="mb-1 block text-xs font-medium text-slate-500">数据年份</label>
              <input className="of-input" placeholder="如：2027" value={importYear} onChange={(e) => setImportYear(e.target.value)} />
            </div>
            <div>
              <label className="mb-1 block text-xs font-medium text-slate-500">数据来源</label>
              <input className="of-input" placeholder="如：某机构 2027 届薪酬报告" value={importSource} onChange={(e) => setImportSource(e.target.value)} />
            </div>
          </div>
          <div>
            <label className="mb-1 block text-xs font-medium text-slate-500">基准数据 JSON</label>
            <textarea
              className="of-textarea h-48 resize-none font-mono text-xs"
              placeholder='{"degree_base": {...}, "direction_coef": {...}, "city_tiers": {...}}'
              value={importJson}
              onChange={(e) => setImportJson(e.target.value)}
            />
          </div>
          {importMsg && <p className="text-xs text-rose-600">{importMsg}</p>}
        </div>
      </Modal>
    </div>
  );
};

export default SalaryNegotiation;
