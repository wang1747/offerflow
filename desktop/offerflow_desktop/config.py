"""OfferFlow 桌面版（工作台）全局配置。"""

import os
import sys

APP_NAME = "OfferFlow 求职工作台"
# v2.2.7：两端功能同步 —— 补齐昵称/改密码/语料开关/用量成本/简历版本回滚/
#         信息充足度/后端投递提醒，并新增「AI 用量与成本」与「管理后台」（8 个模块）
# v2.2.8：修复三个高频操作的崩溃 —— 「导入简历」调用了 api_client 里并不存在的
#         save_resume（简历根本无法入库）、简历生成页与优化页的「导出」调用了不存在的
#         resolve_export_dir。改为 api_client.upload_resume（异步执行，避免在模态框内
#         发请求导致响应晚于对话框销毁）与本地 database.resolve_export_dir。
# v2.2.14：本地 AI 落地（P0~P4）—— 文档解析（LiteParse + Windows 系统 OCR）、
#         四路语料拉取 + 本地 embedding（fastembed + bge-small-zh）+ 自研 SQLite
#         向量索引、生成前统一走本地检索（断网可用）、随包种子索引（开箱即有离线检索）、
#         「本地知识库」页，以及**本地可写主存 + 30 分钟双向同步**（离线也能记投递，
#         联网自动补传，靠客户端 id 保证幂等不重复）。
# v2.2.15：本地 AI 补上图谱与记忆（P3b/P3c），并把「离线可写」推广到 JD 与面经：
#         · 本地实体-关系图谱：用你自己的模型抽取实体关系，检索时做**跨资料** 1 跳
#           展开（例：命中 JD 时自动带出你简历里与它共享的技能节点）；
#         · 本地记忆：从你的资料提炼「关于你本人的稳定事实」，生成前按 query 召回注入；
#         · 离线也能存 JD / 面经（联网后自动上行，服务端按客户端 id 幂等，命中即在不
#           调模型之前返回 —— 重放不会二次花模型钱）；
#         · 面经列表按全部分页拉取合并（只拉首页会把第 2 页往后的本地记录删干净）。
# v2.2.16：模块独立可用 —— 「面试准备 / 薪资谈判 / 岗位匹配」三页不再要求你先跑去
#         「简历生成」准备简历：
#         · 三页各加「导入简历」按钮（复用同一份 `ui/import_resume.py`，原本长在
#           简历生成页里），没有简历时就地选 Word/PDF 导入，本机解析、不上传原文件；
#         · 缺简历时给出引导条并**直接禁用主按钮**（原来是"点了才弹错"）；
#         · 导入成功后立刻刷新下拉并**自动选中**新简历（等入库成功再回调，避免
#           "值是新的、选项里还没有"的空档）；
#         · 简历下拉重建后按 id 找回原选择（`clear()` 会丢选中，原来每次进页面
#           都会被重置成第一份）。
# v2.2.26：修「本地知识库的抽取图谱 / 提炼记忆一直提示未配置模型」——
#         桌面端此前有两套互不相通的模型配置：「模型设置」写的是**云端账号**
#         （Key 服务端加密、只回掩码），而本地 AI 读的是**本地 SQLite**，写本地
#         那份的函数从来没有调用点 ⇒ 用户怎么配都提示未配置。现改为：保存成功后
#         同步一份到本地（`local_model.*`），并在本地知识库页加「本地模型…」入口
#         （可单独配、支持无 Key 的本机 Ollama）。
# v2.2.28：新增「生成守门层」（core/generation_guard.py）—— 简历生成完成后就地体检，
#         四类判据：编造核对（拿不到原始素材时降级报「无法核对」而不是静默通过）、
#         中文求职风险（外包/OD 标签、练习型题材、精通无证据、时间线冲突）、AI 腔、
#         量化引导。结果区新增「生成守门 N 分」卡片，命中有 high 级编造时标红提醒
#         「投递前必须逐条核对」。同时服务端两条链路的提示词各加了量化三级梯度、
#         决策-权衡写法与中文求职雷区（生成侧 + 优化侧，已随服务端上线）。
# v2.3：网页版 P1 缺口补齐（G8/G9/G10）——
#   G8 薪资基准：谈判页顶部显示「市场数据 vN · YYYY 届」信息条，支持「刷新市场数据」
#      与「导入权威数据」（JSON，写基准；**v2.4 起改为写本机**，不再动服务端全局表）；
#   G9 成本调用链下钻：成本看板每行可展开，拉取该次调用明细后画瀑布图 + span 明细表；
#   G10 首页速览：新增「最近投递」（最近 6 条 + 状态徽标）与「匹配速览」
#      （Top4 + 分数色阶，无匹配记录时整卡隐藏）。
# v2.3.1：修复简历「生成 / 优化」链路的 8 处已核实缺陷（桌面侧）——
#   ① 守门层读错经历键（外包经历 / 玩具项目两条风险判据此前恒不触发）；
#   ② 改排序 / 换候选后结果字段被整体替换丢失（换候选后「换 3 个写法」失败）；
#   ③ 优化页构造期就在发网络请求（页面提前销毁时后台线程摸到已删除对象）；
#   ④ 历史记录里质量字段键名错位（review / quality）；
#   ⑤ 分数为空时格式化抛 TypeError；
#   ⑥ 空白卡片被当成已填内容提交；
#   ⑦ 风险核对用了「完成时刻」的 JD（应冻结为发起时那份）；
#   ⑧ 优化稿不过守门 + 优化失败仍显示「已自动保存」。
# v2.4：AI 能力全面本地化（生成 / 优化 / 面试 / 匹配 / 薪资五模块默认走本机）——
#   ① 新增 `core/` 判据层 5 模块：sufficiency（输入充分度 → 动态篇幅）、page_fit（一页硬约束）、
#      quality（质量评分 + 重试反馈）、fidelity（bullet 级保真）、skill_keywords（技能词典）；
#   ② 生成主流程重写为 11 步同步编排（事实回填 / 保真 / 编造剔除 / 数字兜底 / 一页 fail-closed /
#      质量重试 / 语义评分），补齐此前缺失的 8 项护栏（此前会编造学校名与公司名）；
#   ③ 优化阈值 0.60 → 0.35（同义改写不再被误判「丢内容」而回退原文）+ 新增事实拦截；
#   ④ 面试验会话状态机（8 题上限，简历/JD 原文冻结进会话）+ 离线本机抽题与去重；
#   ⑤ 匹配改本机计算 + 结果落本机 + 排行榜「本机优先 ∪ 云端」；
#   ⑥ 薪资新增**开口价**（此前只有目标价/底线，解读里「怎么开口」没有数字依据）；
#      基准数据改本机保存与版本管理（`core/salary_benchmark_store`），导入时提示未识别字段；
#   ⑦ 修 `ui/pages/salary._finish_negotiation` 漏 `worker.start()`（谈判总结此前永不出现）。
# v2.5：简历生成/优化「复用闭环」——每次必查库、成果自动入池，越用越准。
#   ① 双池召回（`local_ai/index.py`）：成果池 `source ∈ {generated, optimized}` 可被后续同类
#      生成/优化召回；材料池 `source="resume"` 维持排除，防与用户自己的输入自引用
#      （实测 query 与自有简历 score=1.000，排除是对的）。
#   ② 成果自动入池：生成/优化完成即 `upsert_local` 写本机索引，无需用户手动保存；
#      重建（`rebuild`）与增量共用同一套来源规则，成果不会被快照以 resume 顶掉。
#   ③ 质量闸门 `WORKS_MIN_QUALITY=60`：不合格成果降级回材料池而非丢弃；无质量报告放行。
#   ④ 回声防护 `ECHO_SCORE_CEILING=0.95`：成果池中与当前输入几乎同分的块丢弃（同一底稿
#      历史版本），防模型照抄；范文语料实测最高 0.765 不受影响。
#   ⑤ 两路各自带配额（成果与输入同源、分天然高，不与 corpus 范文合成一路排序）。
#   ⑥ 召回轨迹（`retrieval`：query / corpus_hits / work_hits / echo_dropped / backend /
#      threshold / error）随结果落库并回前端，是「这次到底查了没有」的可验证凭据。
# v2.5.2：修复自动更新弹黑窗——旧 launch_apply_and_exit 用 DETACHED_PROCESS 拉起
#         _apply_update.bat，cmd.exe 无控制台会自建可见窗（Win11 由 Windows Terminal
#         接管）→ 升级时弹 findstr 黑窗。改为 CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP
#         （隐藏控制台 + 脱离父进程组），stdio 全丢。判据见 test_updater_incremental 第 7 节。
# v2.5.5：AI 能力补全 + 三处匹配缺陷根治（判据见 test_ai_local_judges /
#         test_salary_benchmark，产物层见 _t_build_code_check_255.py）。
#   ① 简历侧补齐 `ai` 方向（`services/resume_generation.JOB_TEMPLATES` +
#      `core/skill_keywords.DIRECTION_SKILLS`，两处逐字同序，由 test 锁死）——
#      此前 15 方向独缺 AI，而面试库全 AI、薪资侧早有 ai 系数，自相矛盾；
#      连带修复「方向高频词覆盖」判据的静默跳过（`top_keywords("ai")` 由空变非空）。
#   ② 薪资 `DIRECTION_COEF["ai"]` 由 1.70 校准为 1.45：1.70 是「全市场学历分布」口径下
#      硕士流向高薪岗的溢价，套到本科应届会把「AI+本科」估成 10.2k~15.3k（目标价顶格）；
#      1.45 使普通本科 AI 应届落回 8.5k~13.0k（目标价 13k，落在合理区间）。
#   ③ `core/text_utils.alias_in_text` 改为**大小写不敏感**：此前把展示名（Python /
#      PyTorch / RAG …）拿去比对已小写化的文本 ⇒ 恒判未命中，16 个方向共有的既存缺陷。
#      `core/quality._score_ats` 同时改走 `SKILL_ALIASES` 别名扩展（LLM↔大模型 等）。
#   ④ `core/salary_benchmark.resolve_direction` 的 ASCII 关键词改**词边界**匹配：
#      此前 `if kw in lowered` 让 `ai` 命中 `email`/`training`、`ic` 命中 `curriculum`
#      ⇒ 非 AI 岗被白套高系数。另删 `智能` 宽词（智能制造→机械而非 ai）、补英文岗位词、
#      桥接简历侧/薪资侧同义异名（environmental ↔ environment）。
#   ⑤ `services.salary_negotiation.get_salary_reference` 的「画像提取」补 try/except：
#      本机模型不可用时降级为「简历原文跑规则识别」，返回 `degraded=True` 并如实告知，
#      不再整块抛异常（此前与同函数「解读」步骤的降级语义、与本模块「数字由规则算」冲突）。
APP_VERSION = "2.5.6"

# 应用根目录（源码运行 = 项目根；打包后 = 可执行文件目录）


def _app_root() -> str:
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


APP_ROOT = _app_root()


def _data_dir() -> str:
    """数据目录（登录态、窗口记忆等）。

    - 打包后（安装版）：写到 %LOCALAPPDATA%\\OfferFlow\\data，避免装在
      Program Files 时无写权限，且卸载/升级不丢用户数据；
    - 源码运行：仍放项目 data/，方便开发与备份。
    """
    if getattr(sys, "frozen", False):
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
        return os.path.join(base, "OfferFlow", "data")
    return os.path.join(APP_ROOT, "data")


DATA_DIR = _data_dir()
os.makedirs(DATA_DIR, exist_ok=True)

DB_PATH = os.path.join(DATA_DIR, "offerflow.db")

# 模型预设（BYOK）：与 Web 版保持一致
PROVIDER_PRESETS = [
    {"key": "deepseek", "label": "DeepSeek", "base_url": "https://api.deepseek.com/v1", "model": "deepseek-chat"},
    {"key": "openai", "label": "OpenAI", "base_url": "https://api.openai.com/v1", "model": "gpt-4o-mini"},
    {"key": "moonshot", "label": "Moonshot（月之暗面）", "base_url": "https://api.moonshot.cn/v1", "model": "moonshot-v1-8k"},
    {"key": "custom", "label": "自定义（OpenAI 兼容）", "base_url": "", "model": ""},
]

# 简历生成长度约束（与 Web 版一致）
MIN_RESUME_LENGTH = 700

# 薪资谈判最大轮数已移到 `services.salary_negotiation.MAX_ROUNDS` ——
# 那是**真正执行上限**的地方（会话状态机在里面判断收口），UI 只做展示。
# 常量留在两处就等于「两份真源」：改了执行侧、忘改展示侧，界面会写「最多 5 轮」
# 而实际跑 3 轮（或反过来），且没有任何报错。

# 投递状态（与 Web 版一致的九态流转）
APPLICATION_STATUSES = [
    "saved", "applied", "online_test", "first_interview",
    "second_interview", "hr_round", "offered", "accepted", "rejected",
]

STATUS_LABELS = {
    "saved": "已收藏",
    "applied": "已投递",
    "online_test": "笔试",
    "first_interview": "一面",
    "second_interview": "二面",
    "hr_round": "HR 面",
    "offered": "已发 Offer",
    "accepted": "已接受",
    "rejected": "已挂",
}

STATUS_COLORS = {
    "saved": "#94a3b8",
    "applied": "#3b82f6",
    "online_test": "#8b5cf6",
    "first_interview": "#f59e0b",
    "second_interview": "#f97316",
    "hr_round": "#ec4899",
    "offered": "#10b981",
    "accepted": "#059669",
    "rejected": "#ef4444",
}

# 深色主题下的同一套状态色（每个状态保持**可区分的色相**，
# 但整体提亮以在深底 #1e1e26 上达到 WCAG AA ≥ 4.5:1）。
# 与 STATUS_COLORS 必须一一对应，新增状态时两边都要加。
STATUS_COLORS_DARK = {
    "saved": "#9ca3af",
    "applied": "#60a5fa",
    "online_test": "#a78bfa",
    "first_interview": "#fbbf24",
    "second_interview": "#fb923c",
    "hr_round": "#f472b6",
    "offered": "#34d399",
    "accepted": "#4ade80",
    "rejected": "#f87171",
}
