import { useCallback, useEffect, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { api } from "../api/client";
import type { JDItem, JDParseResult } from "../types";
import { Card, SectionTitle, Badge, EmptyState, Loading, Icons } from "../components/ui";
import { useStageProgress } from "../hooks/useStageProgress";

const formatTime = (iso: string) => {
  const d = new Date(iso);
  return `${d.toLocaleDateString()} ${d.toLocaleTimeString()}`;
};

export default function JDAnalysis() {
  const [rawText, setRawText] = useState("");
  const [imageFile, setImageFile] = useState<File | null>(null);
  const [loading, setLoading] = useState(false);
  const [result, setResult] = useState<JDParseResult | null>(null);
  const [parsedJdId, setParsedJdId] = useState("");
  const navigate = useNavigate();

  const parseStage = useStageProgress(
    ["正在识别岗位信息…", "正在提取技能要求…", "正在分析隐藏信号…"],
    loading,
  );
  const [error, setError] = useState<string | null>(null);
  const [history, setHistory] = useState<JDItem[]>([]);
  const [historyLoading, setHistoryLoading] = useState(true);
  const [editingId, setEditingId] = useState<string | null>(null);
  const [editCompany, setEditCompany] = useState("");
  const [editPosition, setEditPosition] = useState("");
  const [savingEdit, setSavingEdit] = useState(false);
  const autoCloseTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  const loadHistory = useCallback(async () => {
    setHistoryLoading(true);
    try {
      const resp = await api.jd.list();
      if (resp.success && resp.data) setHistory(resp.data);
    } catch (err) {
      console.error("加载历史列表失败:", err);
    } finally {
      setHistoryLoading(false);
    }
  }, []);

  useEffect(() => { void loadHistory(); }, [loadHistory]);
  useEffect(() => () => { if (autoCloseTimer.current) clearTimeout(autoCloseTimer.current); }, []);

  const showError = (msg: string) => {
    setError(msg);
    if (autoCloseTimer.current) clearTimeout(autoCloseTimer.current);
    autoCloseTimer.current = setTimeout(() => setError(null), 5000);
  };

  const handleParse = async () => {
    const trimmed = rawText.trim();
    if (!trimmed) { showError("请粘贴职位要求"); return; }
    setLoading(true); setError(null); setResult(null);
    try {
      const response = await api.jd.parse(trimmed);
      if (response.success && response.data?.parsed) {
        const parsed = response.data.parsed;
        if (Object.keys(parsed).length === 0) {
          showError("解析结果为空，请检查职位要求内容");
          setResult(null);
        } else {
          setResult(parsed);
          setParsedJdId(response.data.id || "");
        }
        await loadHistory();
        setRawText("");
      } else {
        showError(response.error || "解析失败");
      }
    } catch (err) {
      console.error("解析失败:", err);
      showError("请求失败，请确保后端服务已启动");
    } finally {
      setLoading(false);
    }
  };

  const handleOcr = async () => {
    if (!imageFile) { showError("请选择截图图片"); return; }
    setLoading(true); setError(null); setResult(null);
    try {
      const response = await api.jd.ocr(imageFile);
      if (response.success && response.data) {
        setResult(response.data.parsed);
        setRawText(response.data.ocr_text || "");
        await loadHistory();
        setImageFile(null);
      } else {
        showError(response.error || "OCR 识别失败");
      }
    } catch (err) {
      showError(err instanceof Error ? err.message : "OCR 识别失败");
    } finally {
      setLoading(false);
    }
  };

  const handleDelete = async (id: string) => {
    if (!confirm("确定要删除这条职位要求吗？")) return;
    try {
      await api.jd.delete(id);
      await loadHistory();
    } catch (err) {
      console.error("删除失败:", err);
      showError("删除失败，请重试");
    }
  };

  const startEdit = (item: JDItem) => {
    setEditingId(item.id);
    setEditCompany(item.company || "");
    setEditPosition(item.position || "");
  };

  const cancelEdit = () => {
    setEditingId(null);
    setEditCompany("");
    setEditPosition("");
  };

  const handleSaveEdit = async (id: string) => {
    const company = editCompany.trim();
    const position = editPosition.trim();
    if (!company && !position) { showError("公司或职位至少填写一项"); return; }
    setSavingEdit(true);
    try {
      await api.jd.update(id, { company, position });
      await loadHistory();
      cancelEdit();
    } catch (err) {
      console.error("保存失败:", err);
      showError("保存失败，请重试");
    } finally {
      setSavingEdit(false);
    }
  };

  const handleSelectHistory = (item: JDItem) => {
    setRawText(item.raw_text || "");
    setResult(null);
    setError(null);
  };

  const renderTechStack = (techStack: JDParseResult["tech_stack"]) => {
    if (!techStack || Object.keys(techStack).length === 0)
      return <span className="text-sm text-slate-400">无</span>;
    return (
      <div className="flex flex-wrap gap-2">
        {Object.entries(techStack).map(([key, values]) =>
          values && values.length > 0 ? (
            <Badge key={key} color="indigo">{key}: {values.join(", ")}</Badge>
          ) : null
        )}
      </div>
    );
  };

  const hasResultData = result && Object.keys(result).length > 0;

  return (
    <div>
      {error && <div className="of-alert-error mb-4">{Icons.alert}<span>{error}</span></div>}

      <Card className="mb-5 p-5">
        <SectionTitle>粘贴职位要求</SectionTitle>
        <textarea
          className="of-textarea h-48"
          placeholder="粘贴招聘要求文本..."
          value={rawText}
          onChange={(e) => setRawText(e.target.value)}
          disabled={loading}
          aria-label="JD 文本输入"
        />
        <div className="mt-3 flex flex-wrap items-center gap-3">
          <button onClick={handleParse} disabled={loading || !rawText.trim()} className="of-btn-primary">
            {loading ? (
              <><span className="h-4 w-4 animate-spin rounded-full border-2 border-white/30 border-t-white" />{parseStage}</>
            ) : <>开始解析</>}
          </button>
          <input
            type="file"
            accept=".png,.jpg,.jpeg,.bmp,.webp"
            onChange={(e) => setImageFile(e.target.files?.[0] || null)}
            className="block text-sm text-slate-500 file:mr-3 file:rounded-lg file:border-0 file:bg-brand-50 file:px-3 file:py-1.5 file:text-brand-600"
          />
          <button onClick={handleOcr} disabled={loading || !imageFile} className="of-btn-outline">
            OCR 识别导入
          </button>
        </div>
      </Card>

      {hasResultData && (
        <Card className="mb-5 border-emerald-200 bg-emerald-50/50 p-5">
          <div className="mb-4 flex items-center justify-between">
            <h2 className="flex items-center gap-2 text-lg font-bold text-emerald-700">
              <span className="flex h-6 w-6 items-center justify-center rounded-full bg-emerald-500 text-white">
                <svg width="14" height="14" viewBox="0 0 16 16" fill="none">
                  <path d="M13 4L6 11l-3-3" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" />
                </svg>
              </span>
              解析结果
            </h2>
            <button onClick={() => setResult(null)} className="text-sm text-slate-400 hover:text-slate-600">关闭</button>
          </div>
          <div className="space-y-3 text-sm">
            <div className="flex gap-2">
              <span className="w-20 flex-shrink-0 font-medium text-slate-500">公司</span>
              <span className="text-slate-900">{result.company || "-"}</span>
            </div>
            <div className="flex gap-2">
              <span className="w-20 flex-shrink-0 font-medium text-slate-500">职位</span>
              <span className="text-slate-900">{result.position || "-"}</span>
            </div>
            <div>
              <span className="font-medium text-slate-500">硬性要求</span>
              <div className="mt-1.5 flex flex-wrap gap-1.5">
                {result.must_have?.length ? result.must_have.map((item, i) => (
                  <Badge key={i} color="red">{item}</Badge>
                )) : <span className="text-slate-400">无</span>}
              </div>
            </div>
            <div>
              <span className="font-medium text-slate-500">加分项</span>
              <div className="mt-1.5 flex flex-wrap gap-1.5">
                {result.nice_to_have?.length ? result.nice_to_have.map((item, i) => (
                  <Badge key={i} color="amber">{item}</Badge>
                )) : <span className="text-slate-400">无</span>}
              </div>
            </div>
            <div>
              <span className="font-medium text-slate-500">技术栈</span>
              <div className="mt-1.5">{renderTechStack(result.tech_stack)}</div>
            </div>
            {result.hidden_signals?.length > 0 && (
              <div>
                <span className="font-medium text-slate-500">隐藏信号</span>
                <div className="mt-1.5 flex flex-wrap gap-1.5">
                  {result.hidden_signals.map((item, i) => (
                    <Badge key={i} color="indigo">{item}</Badge>
                  ))}
                </div>
              </div>
            )}
          </div>
          {parsedJdId && (
            <div className="mt-4 flex gap-2 border-t border-emerald-100 pt-3">
              <button
                onClick={() => navigate(`/resume?jd_id=${parsedJdId}`)}
                className="of-btn-outline flex-1 py-1.5 text-sm"
              >
                针对此岗位优化简历
              </button>
            </div>
          )}
        </Card>
      )}

      <div>
        <SectionTitle count={history.length}>历史记录</SectionTitle>
        {historyLoading ? (
          <Loading text="加载中..." />
        ) : history.length === 0 ? (
          <EmptyState title="暂无职位记录" description="粘贴招聘要求，AI 帮你拆出重点" />
        ) : (
          <div className="space-y-2">
            {history.map((item) => (
              <div
                key={item.id}
                className="of-card-hover p-4 cursor-pointer"
                onClick={() => handleSelectHistory(item)}
              >
                {editingId === item.id ? (
                  <div className="flex flex-wrap items-center gap-2" onClick={(e) => e.stopPropagation()}>
                    <input
                      className="of-input w-44"
                      placeholder="公司"
                      value={editCompany}
                      onChange={(e) => setEditCompany(e.target.value)}
                      aria-label="编辑公司"
                    />
                    <input
                      className="of-input w-44"
                      placeholder="职位"
                      value={editPosition}
                      onChange={(e) => setEditPosition(e.target.value)}
                      aria-label="编辑职位"
                    />
                    <button
                      onClick={() => handleSaveEdit(item.id)}
                      disabled={savingEdit}
                      className="of-btn-primary px-3 py-1 text-xs"
                    >
                      {savingEdit ? "保存中..." : "保存"}
                    </button>
                    <button
                      onClick={cancelEdit}
                      className="of-btn-outline px-3 py-1 text-xs"
                    >
                      取消
                    </button>
                  </div>
                ) : (
                  <div className="flex items-center justify-between">
                    <div className="min-w-0 flex-1">
                      <span className="text-sm font-medium text-slate-900">{item.company || "未知公司"}</span>
                      <span className="mx-2 text-slate-300">·</span>
                      <span className="text-sm text-slate-600">{item.position || "未知职位"}</span>
                      <span className="ml-3 hidden text-xs text-slate-400 sm:inline">
                        {formatTime(item.created_at || "")}
                      </span>
                    </div>
                    <div className="flex flex-shrink-0 items-center gap-2">
                      <button
                        onClick={(e) => { e.stopPropagation(); startEdit(item); }}
                        className="of-btn-outline px-2.5 py-1 text-xs"
                      >
                        编辑
                      </button>
                      <button
                        onClick={(e) => { e.stopPropagation(); handleDelete(item.id); }}
                        className="of-btn-danger px-2.5 py-1 text-xs"
                      >
                        删除
                      </button>
                    </div>
                  </div>
                )}
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
