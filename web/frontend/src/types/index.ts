// ===== 通用 =====
export interface ApiResponse<T> {
  success: boolean;
  data: T | null;
  error: string | null;
}

// ===== 认证 =====
export interface User {
  id: string;
  name: string;
  email?: string;
  role?: string;
  is_active?: boolean;
  avatar?: string | null;
  created_at: string;
}

export interface AdminUser {
  id: string;
  name: string;
  email?: string;
  role: string;
  is_active: boolean;
  created_at: string;
}

export interface LoginResponse {
  access_token: string;
  token_type: string;
  user: User;
}

// ===== JD =====
export interface JDParseResult {
  company?: string;
  position?: string;
  must_have: string[];
  nice_to_have: string[];
  tech_stack: {
    backend: string[];
    frontend: string[];
    infra: string[];
    other: string[];
  };
  hidden_signals: string[];
}

export interface JDItem {
  id: string;
  company?: string;
  position?: string;
  raw_text?: string;
  must_have?: string[];
  nice_to_have?: string[];
  tech_stack?: Record<string, string[]>;
  hidden_signals?: string[];
  created_at?: string;
}

// ===== 简历 =====
export interface ResumeItem {
  id: string;
  version: number;
  raw_text: string;
  source_file?: string | null;
  parsed_json?: {
    sections?: string[];
    skills?: string[];
    changes?: ChangeItem[];
    kind?: string;
    parent_id?: string;
    target_jd?: { id?: string | null; company?: string; position?: string };
    added_keywords?: string[];
    generation?: ResumeGenerationResult;
  } | null;
  created_at?: string;
  version_count?: number;
  document_id?: string;
}

export interface AtsResult {
  score: number;
  issues: string[];
  suggestions: string[];
  matched_keywords?: string[];
  missing_keywords?: string[];
}

export interface ChangeItem {
  section: string;
  before: string;
  after: string;
  reason: string;
}

export interface OptimizeResult {
  optimized: string;
  changes: ChangeItem[];
  added_keywords: string[];
  removed_keywords: string[];
  removed: string[];
  length_warning: string;
  gap: {
    matched: string[];
    missing: string[];
    partial: string[];
  };
  target_jd: { id: string | null; company: string; position: string };
  ats: AtsResult;
  ats_after: AtsResult;
  preservation: {
    score: number;
    passed: boolean;
    fallback: boolean;
    missing_facts: string[];
    added_facts?: string[];
    critical_facts?: Record<string, number>;
  };
  edits_applied?: number;
  new_version: { id: string; version: number } | null;
}

export interface ResumeGenerationContact {
  phone?: string;
  email?: string;
  github?: string;
}

export interface ResumeGenerationEducation {
  school: string;
  major: string;
  degree: string;
  start: string;
  end: string;
  courses?: string;
  gpa?: string;
  honors?: string;
  detail?: string;
}

export interface ResumeGenerationExperience {
  type: "project" | "internship" | "work";
  name: string;
  role: string;
  start: string;
  end: string;
  bullets: string[];
}

export interface ResumeGenerationResult {
  id: string;
  version: number;
  /** 所属简历文档 id：同一份简历的多个版本共用一个 document_id */
  document_id?: string;
  resume_text: string;
  name: string;
  position: string;
  contact: ResumeGenerationContact;
  summary: string;
  education: ResumeGenerationEducation[];
  experiences: ResumeGenerationExperience[];
  skills: string[];
  certifications: string[];
  sections: string[];
  tips: string[];
  risks?: Array<{
    section: string;
    level: string;
    issue: string;
    suggestion: string;
  }>;
  jd_alignment?: Array<{
    exp: number;
    keywords: string[];
  }>;
  reference_source?: string;
  ats: AtsResult;
  gap: {
    matched: string[];
    missing: string[];
    partial: string[];
  };
  fidelity?: {
    passed: boolean;
    added_skills: string[];
    added_certifications: string[];
    added_contacts: string[];
    added_schools: string[];
    added_companies: string[];
  };
  quality?: ResumeQualityReport;
  semantic?: ResumeSemanticReport;
  sufficiency?: ResumeSufficiencyReport;
}

export interface ResumeSemanticReport {
  total_score?: number;
  result_credibility?: { score: number; reason: string };
  expression_quality?: { score: number; reason: string };
  jd_fit?: { score: number; reason: string };
  overall?: string;
}

export interface ResumeSufficiencyReport {
  score: number;
  level: string;
  target_length: number;
  min_length: number;
  gaps: string[];
  message: string;
}

export interface ResumeQualityReport {
  total_score: number;
  passed: boolean;
  dimensions: Record<
    string,
    { score: number; weight: number; label: string }
  >;
  violations: Array<{
    section: string;
    dimension: string;
    issue: string;
    suggestion: string;
  }>;
  suggestions: string[];
}

export interface RecommendSkillsResult {
  direction?: string;
  skills?: string[];
  directions?: Record<string, string[]>;
}

// ===== 匹配 =====
export interface MatchResult {
  id?: string;
  jd_id: string;
  resume_id: string;
  company?: string;
  position?: string;
  score: number;
  skill_match_detail?: {
    matched?: string[];
    missing?: string[];
    partial?: string[];
  };
  gap_analysis?: {
    hard_gap?: string[];
    soft_gap?: string[];
    suggestion?: string;
  };
  suggestion?: string;
}

// ===== 投递 =====
export interface Application {
  id: string;
  company: string;
  position: string;
  status: string;
  jd_id?: string;
  resume_id?: string;
  applied_date?: string;
  next_action?: string;
  next_action_date?: string;
  notes?: string;
  created_at?: string;
  updated_at?: string;
}

export type ApplicationStatus =
  | "saved"
  | "applied"
  | "online_test"
  | "first_interview"
  | "second_interview"
  | "hr_round"
  | "offered"
  | "accepted"
  | "rejected";

export interface ApplicationStats {
  total: number;
  status_counts: Record<string, number>;
  status_order: ApplicationStatus[];
  conversion_rate: {
    applied_to_interview: number;
    interview_to_offer: number;
  };
}

export interface ReminderItem {
  id: string;
  company: string;
  position: string;
  status: string;
  next_action?: string;
  next_action_date?: string;
  days_until?: number;
  updated_at?: string;
  days_since_update?: number;
}

export interface Reminders {
  overdue: ReminderItem[];
  overdue_count: number;
  upcoming: ReminderItem[];
  upcoming_count: number;
  has_reminders: boolean;
}

// ===== 面试 =====
export interface InterviewArticle {
  id: string;
  company: string;
  position?: string | null;
  raw_content: string;
  source: string;
  questions?: string[] | null;
  tags?: string[] | null;
  difficulty?: string | null;
  created_at?: string;
}

export interface InterviewQuestion {
  id: string;
  article_id?: string | null;
  question: string;
  answer?: string;
  category?: string;
  difficulty?: string;
}

export interface GeneratedQuestion {
  question: string;
  category: string;
  difficulty: string;
  answer?: string;
  suspicious_numbers?: string[];
}

export interface SimulateSession {
  session_id: string;
  question: string;
  question_number: number;
}

export interface SimulateAnswer {
  feedback: string;
  next_question: string | null;
  is_finished: boolean;
  question_number: number;
}

// ===== 薪资谈判 =====
export interface NegotiationStart {
  session_id: string;
  hr_message: string;
  round_count: number;
}

export interface NegotiationAnswer {
  coaching: string;
  next_hr_message: string | null;
  is_finished: boolean;
  round_count: number;
}

export interface NegotiationMessage {
  role: "hr" | "user";
  content: string;
}

export interface NegotiationCoaching {
  hr: string;
  answer: string;
  coaching: string;
}

export interface NegotiationSummary {
  summary: string;
  round_count: number;
  messages: NegotiationMessage[];
  coaching: NegotiationCoaching[];
  status: string;
  scenario: string;
  target_salary: string | null;
  bottom_salary: string | null;
}

export interface SalaryReference {
  salary_range: { low: number; high: number };
  suggest_ask: number;
  suggest_target: number;
  suggest_bottom: number;
  degree: string;
  school_tier: string;
  direction: string;
  position: string;
  city: string;
  city_tier: string;
  highlights: string[];
  analysis: string;
  market_basis: string;
  sources: string[];
  data_version: number;
  data_year: string;
  data_source: string;
  data_updated_at: string | null;
  caveats: string[];
}

export interface SalaryBenchmarkInfo {
  version: number;
  data_year: string;
  source: string;
  note: string;
  updated_at: string | null;
}

// ===== 模型设置（BYOK） =====
export interface PresetProvider {
  key: string;
  base_url: string;
  model: string;
}

export interface ModelConfig {
  provider: string;
  base_url: string;
  model: string;
  api_key_masked: string;
  has_config: boolean;
  using_default: boolean;
}

// ===== 可观测（成本看板 / 执行追踪） =====
export interface TraceItem {
  id: string;
  operation: string;
  status: string;
  total_tokens: number;
  total_cost: number;
  /** 人民币折算值（后端按参考汇率换算，展示用） */
  total_cost_cny: number | null;
  duration_ms: number;
  span_count: number;
  created_at: string | null;
}

export interface SpanItem {
  id: string;
  run_id: string | null;
  model: string | null;
  prompt_tokens: number;
  completion_tokens: number;
  total_tokens: number;
  cost: number | null;
  /** 人民币折算值；cost 为 null 时同样为 null（表示无法估算） */
  cost_cny: number | null;
  duration_ms: number;
  start_offset_ms: number;
  status: string;
}

export interface TraceDetail {
  trace: TraceItem;
  spans: SpanItem[];
}

export interface OperationCost {
  operation: string;
  cost: number;
  cost_cny: number | null;
  tokens: number;
  count: number;
}

export interface ModelCost {
  model: string;
  cost: number;
  cost_cny: number | null;
  tokens: number;
  count: number;
}

export interface CostSummary {
  total_cost: number;
  total_cost_cny: number | null;
  total_tokens: number;
  trace_count: number;
  by_operation: OperationCost[];
  by_model: ModelCost[];
}

export interface CommunityAttachment {
  name: string;
  kind: "image" | "doc";
  original_name: string;
  size: number;
  url: string;
}

export interface CommunityPost {
  id: string;
  user_id: string;
  author: string;
  author_avatar?: string | null;
  title: string;
  content: string;
  category: string;
  tags: string[];
  attachments: CommunityAttachment[];
  like_count: number;
  comment_count: number;
  created_at: string;
}

export interface CommunityComment {
  id: string;
  user_id: string;
  author: string;
  author_avatar?: string | null;
  content: string;
  created_at: string;
}

export interface CommunityPostDetail extends CommunityPost {
  comments: CommunityComment[];
}

export interface Feedback {
  id: string;
  user_id: string;
  author: string;
  category: string;
  title: string;
  content: string;
  status: string;
  admin_reply: string | null;
  like_count: number;
  created_at: string;
  updated_at: string;
}

// ===================== 管理后台 =====================

export interface AdminUserUsageStat {
  calls: number;
  tokens: number;
  cost: number;
  /** 人民币折算值（后端按参考汇率换算） */
  cost_cny: number | null;
  last_active_at: string | null;
}

export interface AdminUserRow {
  id: string;
  name: string;
  email: string | null;
  role: string;
  is_active: boolean;
  byok: boolean;
  allow_corpus: boolean;
  llm_model: string | null;
  created_at: string;
  usage: AdminUserUsageStat;
}

export interface AdminUserPage {
  total: number;
  page: number;
  page_size: number;
  items: AdminUserRow[];
}

export interface AdminUserDetail {
  user: {
    id: string;
    name: string;
    email: string | null;
    role: string;
    is_active: boolean;
    byok: boolean;
    allow_corpus: boolean;
    llm_provider: string | null;
    llm_base_url: string | null;
    llm_model: string | null;
    created_at: string;
  };
  usage: {
    calls: number;
    tokens: number;
    cost: number;
    cost_cny: number | null;
    failed: number;
    today_tokens: number;
    quota_daily_tokens: number;
  };
  trend: { date: string; calls: number; cost: number; cost_cny: number | null }[];
  top_operations: { operation: string; calls: number; cost: number; cost_cny: number | null }[];
  content: Record<string, number>;
}

export interface AdminOverview {
  generated_at: string;
  window_days: number;
  users: {
    total: number;
    active: number;
    new_7d: number;
    byok: number;
    admins: number;
    contributing: number;
    active_7d: number;
  };
  usage: {
    calls: number;
    tokens: number;
    cost: number;
    cost_cny: number;
    failed: number;
    fail_rate: number;
    avg_duration_ms: number;
  };
  content: Record<string, number>;
  trend: { date: string; calls: number; cost: number; cost_cny: number | null; tokens: number }[];
  top_users: {
    user_id: string;
    name: string;
    email: string | null;
    byok: boolean;
    calls: number;
    cost: number;
    cost_cny: number | null;
    tokens: number;
  }[];
  top_operations: {
    operation: string;
    calls: number;
    cost: number;
    cost_cny: number | null;
    avg_duration_ms: number;
  }[];
  alerts: { level: string; message: string }[];
}

export interface AdminUsageItem {
  key: string;
  calls: number;
  tokens: number;
  cost: number;
  cost_cny: number | null;
  avg_duration_ms?: number;
  failed?: number;
  prompt_tokens?: number;
  completion_tokens?: number;
  name?: string;
  email?: string | null;
  byok?: boolean;
}

export interface AdminUsageResult {
  dimension: string;
  items: AdminUsageItem[];
  total?: { calls: number; tokens: number; cost: number; cost_cny: number | null };
}

export interface AdminTraceRow {
  id: string;
  user_id: string | null;
  user_name: string | null;
  operation: string;
  status: string;
  tokens: number;
  cost: number;
  cost_cny: number | null;
  duration_ms: number;
  spans: number;
  created_at: string | null;
}

export interface AdminTracePage {
  total: number;
  page: number;
  page_size: number;
  items: AdminTraceRow[];
}

export interface AdminHealth {
  checked_at: string;
  status: string;
  database: { ok: boolean };
  default_key: { configured: boolean; model: string | null };
  traces: { total: number; last_1h: number; last_24h: number; last_at: string | null };
  recent_failures: AdminTraceRow[];
  slowest: AdminTraceRow[];
}

export interface AdminAuditLog {
  id: string;
  operator_id: string;
  operator_name: string | null;
  action: string;
  target_type: string | null;
  target_id: string | null;
  target_label: string | null;
  detail: string | null;
  ip: string | null;
  created_at: string | null;
}

export interface AdminAuditPage {
  total: number;
  page: number;
  page_size: number;
  actions: string[];
  items: AdminAuditLog[];
}

export interface AdminSettingItem {
  key: string;
  value: unknown;
  description: string | null;
  updated_by: string | null;
  updated_at: string | null;
}

export interface AdminSettingsResult {
  settings: Record<string, AdminSettingItem>;
  platform_model: { model: string | null; key_configured: boolean; base_url?: string };
}

export interface AnnouncementResult {
  enabled: boolean;
  content: string;
}

// ===== 求职 Agent（v0.2-beta：PostgresSaver + 长期记忆 + 确认写）=====
// 字段与后端 career_agent/routes.py 的 Pydantic 视图一一对应；
// 改后端视图时必须同步改这里，否则页面会静默显示空白。
export interface AgentContextRef {
  type: "resume" | "jd" | "application";
  id: string;
  version?: number | null;
  updated_at?: string | null;
}

export interface AgentMissingContext {
  type: string;
  reason: string;
  hint?: string | null;
}

export interface AgentEvidence {
  ref: string;
  source: string;
  summary: string;
  object_version?: string | null;
}

export interface AgentAction {
  type: string;
  title: string;
  priority: number;
  reason: string;
  evidence_refs: string[];
  requires_confirmation: boolean;
}

export interface AgentOutput {
  decision: string | null;
  confidence: number;
  summary: string;
  actions: AgentAction[];
  evidence: AgentEvidence[];
  missing_info: string[];
}

export interface AgentToolCall {
  tool: string;
  status: string;
  error_code?: string | null;
  latency_ms: number;
}

/** 待确认的写操作意图（`RunView.pending_action` / `GET /sessions/{id}/actions`）。
 *
 * `expected_status` 与 `new_status` **都是服务端填的**：页面只负责显示，
 * 不自己拿业务数据拼一份出来——那会让"页面上显示的确认内容"和"真正会写的内容"
 * 有机会不一致。
 */
export interface AgentActionIntent {
  action_id: string;
  /** 契约名，例如 `application.update_status`。 */
  tool: string;
  /** `pending_confirmation` / `confirmed` / `succeeded` / `cancelled` / `expired` / `failed`。 */
  status: string;
  application_id?: string | null;
  expected_status?: string | null;
  new_status?: string | null;
  reason: string;
  expires_at?: string | null;
}

/** 「确认了写操作但没写成」的结构化原因。
 *
 * 页面**按 `code` 分支**（`status_conflict` / `invalid_status` / `write_failed` …），
 * 不去匹配 `message` 里的中文——文案一改就会静默失效。
 */
export interface AgentWriteFailure {
  code: string;
  message: string;
  tool?: string | null;
}

export interface AgentSession {
  session_id: string;
  /** 恒等于 session_id（设计文档 21.6）。前端两者都留，便于直接核对。 */
  thread_id: string;
  entry_source: string;
  context_refs: AgentContextRef[];
  context_hash?: string | null;
  context_version?: string | null;
  missing_context: AgentMissingContext[];
  needs_input: boolean;
  warnings: string[];
  status: string;
  run_count: number;
  last_output?: AgentOutput | null;
  created_at: string;
  updated_at: string;
  resumed: boolean;
  /** beta 起恒为 true（长期记忆已启用）。 */
  memory_enabled: boolean;
  /** 是否有停在 `waiting_confirmation` 的运行——有则必须给出确认入口。 */
  has_pending_action: boolean;
  /** `postgres` / `memory` / `uninitialized`：会话能否跨重启恢复。 */
  checkpointer: string;
}

export interface AgentRun {
  session_id: string;
  /** 一条用户消息 = 一个独立 run_id（21.6）。 */
  run_id: string;
  /** 含 `waiting_confirmation`（beta 新增的"停在等确认"）。 */
  status: string;
  needs_input: boolean;
  output: AgentOutput;
  missing_context: AgentMissingContext[];
  tool_calls: AgentToolCall[];
  context_hash?: string | null;
  latency_ms: number;
  errors: string[];
  /** 非 null ⇒ 这一轮停在等用户确认。 */
  pending_action?: AgentActionIntent | null;
  /** 只有用户确认过且执行成功才为 true。 */
  wrote_business_data: boolean;
  /** 写了但没成功的原因（只读运行与主动取消都为 null）。 */
  write_failure?: AgentWriteFailure | null;
  checkpointer: string;
}

export interface AgentRunTrace {
  run_id: string;
  status: string;
  decision?: string | null;
  latency_ms: number;
  context_hash?: string | null;
  tool_calls: AgentToolCall[];
  errors: string[];
  created_at: string;
}

export interface AgentTrace {
  session_id: string;
  runs: AgentRunTrace[];
}

export interface AgentToolSpec {
  name: string;
  wire_name: string;
  version: number;
  kind: string;
  description: string;
  evidence_policy: string;
  required_scopes: string[];
  confirmation_required: boolean;
  idempotency_required: boolean;
}

/** 长期记忆条目（`GET|POST /memories`）。
 *
 * `proposed` 是**模型推断的待确认项**，不进向量索引、不会被召回；
 * 只有 `confirmed` 才会被 Agent 当事实使用。页面必须把两者分开显示，
 * 否则用户会以为"模型猜的东西"已经是记录在案的事实。
 */
export interface AgentMemory {
  memory_id: string;
  memory_type: string;
  /** `proposed` / `confirmed` / `superseded` / `rejected` / `expired`。 */
  status: string;
  content: string;
  source: string;
  confidence: number;
  created_at?: string | null;
  expires_at?: string | null;
  superseded_by?: string | null;
  source_refs: string[];
}

/** 业务页面能交给 Agent 的全部东西：只有 ID 与来源，没有正文。 */
export interface AgentEntryContext {
  entry_source: string;
  jd_id?: string | null;
  resume_id?: string | null;
  application_id?: string | null;
}

/** 导出可选版式（后端 `/api/v1/resume/layouts` 的单一真源）。 */
export interface ExportLayoutChoice {
  key: string;
  label: string;
  desc: string;
  /** 1 = 单栏，2 = 双栏。 */
  columns: number;
  /** 双栏时放进副栏的分区名；单栏为空数组。 */
  sidebar_sections: string[];
}
