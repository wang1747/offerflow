"""面试题生成 LangGraph 工作流"""

import json
import logging
import re
from typing import Any, Dict, List, Optional, Tuple, TypedDict

from langgraph.graph import StateGraph, END
from langchain_core.messages import SystemMessage, HumanMessage
from langchain_core.runnables import RunnableConfig
from sqlalchemy.orm import Session

from app.core.llm import get_user_llm_or_raise
from app.core.prompt_guards import INJECTION_GUARD, attach
from app.models.user import User
from app.observability.tracing import trace_operation

logger = logging.getLogger(__name__)

# System Prompt 常量
SYSTEM_PROMPT = """你是一个面试题生成专家。根据候选人的简历、目标JD和面经知识库，生成可能被问到的面试题，并为每道题写一份「能脱稿讲」的参考答案。

要求：
1. 题目要结合简历中的项目经验
2. 题目要覆盖JD中的技术要求
3. 参考面经中的真实问题
4. 区分技术题、项目题、行为题

参考答案的写法（关键）：
- 面向面试口语，用「先给结论/核心逻辑，再展开」的结构，让候选人能脱稿讲、而不是死记硬背
- 结合候选人的真实项目经历，讲清楚「怎么做 + 为什么 + 结果」，不空谈理论
- 技术题：先一句话点明核心，再讲关键原理或选型理由，避免照搬教科书
- 项目题：用 STAR 结构（背景-动作-结果），突出候选人自己的贡献和量化成果
- 行为题：给一个具体事例的讲法框架
- 每条答案 100~250 字，重点突出、不啰嗦

输出格式（JSON）：
{
    "questions": [
        {"question": "题目内容", "category": "技术|项目|行为", "difficulty": "简单|中等|困难", "answer": "参考答案"}
    ]
}"""

# 输入含简历正文、JD 原文与面经（三者都是第三方文本）⇒ 挂防注入。
# 输出是面试题与参考答案，不打分、不产出简历正文 ⇒ 不挂另两条。
SYSTEM_PROMPT = attach(SYSTEM_PROMPT, INJECTION_GUARD)


class InterviewState(TypedDict):
    resume_text: str
    jd_text: str
    article_content: str
    limit: int
    questions: List[dict]
    raw_count: int          # 模型给出的题目条数（含分类/难度不合规被丢掉的）
    dropped_count: int      # 因分类 / 难度格式不合法被丢弃的条数
    error: str


def _clean_json_response(raw: str) -> str:
    """清洗 LLM 输出的 JSON 字符串"""
    raw = re.sub(r"^```json\s*", "", raw.strip())
    raw = re.sub(r"^```\s*", "", raw)
    raw = re.sub(r"```$", "", raw)
    return raw.strip()


# ============ 题目归一（分类 / 难度别名） ============
#
# ⚠️ 旧版是**严格白名单**（`category not in ["技术", "项目", "行为"]` 整条丢弃）：
# 模型只要把「技术」写成「技术题」/「technical」、「中等」写成「一般」/「medium」，
# 整条题就被丢掉；而调用方只把「没有合法题」变成空列表，界面于是显示
# 「未生成题目，请重试」—— 用户完全不知道是**格式**被卡掉了。
# 现在改为「别名归一 + 失败分流」（见 `normalize_question`）。

QUESTION_CATEGORIES = ("技术", "项目", "行为")
QUESTION_DIFFICULTIES = ("简单", "中等", "困难")

#: 分类别名 → 规范值（键一律小写；中文无大小写差异）。
QUESTION_CATEGORY_ALIASES: Dict[str, str] = {
    "技术": "技术", "技术题": "技术", "技术类": "技术", "技术问题": "技术",
    "专业技能": "技术", "专业": "技术", "技能": "技术",
    "tech": "技术", "technical": "技术", "skill": "技术", "skills": "技术",
    "项目": "项目", "项目题": "项目", "项目类": "项目", "项目经验": "项目",
    "项目深挖": "项目", "经历": "项目",
    "project": "项目", "projects": "项目", "experience": "项目",
    "行为": "行为", "行为题": "行为", "行为类": "行为", "行为面试": "行为",
    "综合": "行为", "软技能": "行为", "文化契合": "行为", "人事": "行为",
    "behavior": "行为", "behavioral": "行为", "behaviour": "行为",
    "hr": "行为", "soft": "行为", "softskill": "行为", "softskills": "行为",
}

#: 难度别名 → 规范值。
QUESTION_DIFFICULTY_ALIASES: Dict[str, str] = {
    "简单": "简单", "简单题": "简单", "易": "简单", "基础": "简单",
    "入门": "简单", "初级": "简单",
    "easy": "简单", "basic": "简单", "junior": "简单", "beginner": "简单",
    "中等": "中等", "中等题": "中等", "中等难度": "中等", "中": "中等",
    "一般": "中等", "普通": "中等", "进阶": "中等", "中级": "中等",
    "medium": "中等", "middle": "中等", "normal": "中等", "intermediate": "中等",
    "困难": "困难", "困难题": "困难", "难": "困难", "高难": "困难",
    "高级": "困难", "挑战": "困难", "偏难": "困难",
    "hard": "困难", "difficult": "困难", "senior": "困难", "advanced": "困难",
}


def _norm_enum(value: Any, aliases: Dict[str, str]) -> str:
    """把分类 / 难度别名归一成规范值；认不出来返回空串。"""
    key = str(value or "").strip().lower()
    return aliases.get(key, "") if key else ""


def normalize_question(item: Any) -> Optional[dict]:
    """把模型输出的一条题归一化（别名 → 规范值 + 题干去空白）。

    返回 None 表示**格式不合法**（没有题干，或分类 / 难度认不出来）。
    调用方据此把这条丢掉并**计数** —— 「格式不合法」与「模型根本没给题」
    必须能分开报告，否则用户只能看到一句含糊的「未生成题目，请重试」。
    """
    if not isinstance(item, dict):
        return None
    question = item.get("question")
    if not isinstance(question, str) or not question.strip():
        return None
    category = _norm_enum(item.get("category"), QUESTION_CATEGORY_ALIASES)
    difficulty = _norm_enum(item.get("difficulty"), QUESTION_DIFFICULTY_ALIASES)
    if not category or not difficulty:
        return None
    out = dict(item)
    out["question"] = question.strip()
    out["category"] = category
    out["difficulty"] = difficulty
    return out


def _validate_question(item: dict) -> bool:
    """该条题目能否被归一化（保留旧判据名，供历史调用方使用）。"""
    return normalize_question(item) is not None


# ============ 数字保真（含单位） ============

#: 量化数字 + 紧跟其后的单位。顺序敏感：多字单位必须排在单字之前
#: （`个月` 在 `月` 前、`ms` 在 `s` 前），否则会被截成半个单位。
_NUMBER_RE = re.compile(
    r"\d+(?:\.\d+)?\s*(?:个月|%|万|亿|倍|千|百|GB|MB|KB|TB|ms|QPS|TPS"
    r"|分钟|小时|年|月|日|周|期|秒|天|人|个|条|次|元|s)?"
)

_NUM_CORE_RE = re.compile(r"\d+(?:\.\d+)?")

#: 单位别名 → 规范单位（只做时间类的等价，其余原样保留）。
_UNIT_ALIASES = {
    "个月": "月", "月": "月", "年": "年", "日": "天", "天": "天",
    "周": "周", "期": "期", "小时": "小时", "分钟": "分钟", "秒": "秒", "s": "秒",
}

#: 时间类单位：同一数字配不同时间单位必须判为「对不上」（简历 3 年 ≠ 答案 3 个月）。
_TIME_UNITS = frozenset({"年", "月", "天", "周", "期", "小时", "分钟", "秒"})

#: 中文数字 → 数值（只覆盖常见写法）。
_CN_NUMERALS = {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
                "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}

#: 中文数字后面可能跟的单位（用来把「两段 / 三年」变成 (2, 段) / (3, 年)）。
_CN_UNIT_MULTI = ("小时", "分钟", "个月")
_CN_UNIT_CHARS = "年月日周天个段份次条人名位家篇页行类层步轮台套款项门场季秒"


def _canon_unit(unit: str) -> str:
    u = (unit or "").strip()
    return _UNIT_ALIASES.get(u, u) if u else ""


def _split_num_unit(token: str) -> Tuple[str, str]:
    """把 `"3 个月"` 拆成 ("3", "月")；没读到数字就原样回第一个位置。"""
    m = _NUM_CORE_RE.match(token or "")
    if not m:
        return (token or "").strip(), ""
    return m.group(0), _canon_unit((token or "")[m.end():])


def _number_tokens(text: str) -> List[Tuple[str, str]]:
    """从文本抽 (数字, 规范单位) 列表。"""
    return [_split_num_unit(t.strip()) for t in _NUMBER_RE.findall(text or "") if t.strip()]


def _cn_to_int(raw: str) -> Optional[int]:
    """把「三 / 十 / 十二 / 二十 / 两」这类简单中文数字转成整数；认不出返回 None。"""
    if not raw or any(ch not in _CN_NUMERALS for ch in raw):
        return None
    if len(raw) == 1:
        return _CN_NUMERALS[raw]
    if raw.startswith("十"):
        total, rest = 10, raw[1:]
    elif "十" in raw:
        head, _, tail = raw.partition("十")
        total, rest = _CN_NUMERALS.get(head, 0) * 10, tail
    else:
        return None
    if rest:
        if len(rest) != 1 or rest == "十":
            return None
        total += _CN_NUMERALS[rest]
    return total


def _cn_number_tokens(text: str) -> List[Tuple[str, str]]:
    """从中文表述里抽 (数字, 单位)：`"两段实习"` → `("2", "段")`。

    为什么需要它：简历常写中文数字（两段 / 三年），参考答案写阿拉伯数字（2 段 / 3 年）。
    只比阿拉伯数字会把这种**其实有出处**的数字误报成「简历里没有」——
    这正是旧版「常见数字误报」的主要来源。**只收带单位的写法**（「一线」「一般」这类不算）。
    """
    out: List[Tuple[str, str]] = []
    for m in re.finditer(r"[一二两三四五六七八九十]+", text or ""):
        num = _cn_to_int(m.group(0))
        if num is None:
            continue
        tail = (text or "")[m.end():m.end() + 2]
        unit = ""
        for mu in _CN_UNIT_MULTI:
            if tail.startswith(mu):
                unit = _canon_unit(mu)
                break
        else:
            if tail and tail[0] in _CN_UNIT_CHARS:
                unit = _canon_unit(tail[0])
        if unit:
            out.append((str(num), unit))
    return out


def check_answer_fidelity(resume_text: str, answer: str) -> List[str]:
    """校验参考答案里的数字是否都能在简历原文里找到出处（只标记、不拦截）。

    两条判据（旧版把单位剥掉、只比数字，漏了第二条）：
    - 数字本体在简历里**完全没有** → 标记；
    - 数字本体有，但简历里该数字带的是**别的时间单位**（简历「3 年」、答案「3 个月」）
      → 标记。旧版 `_number_core("3 个月") == "3"` 与简历里的 3 相等，
      等于把「3 年」当成「3 个月」的出处，一处编造被静默放行。

    只做标记、不拦截 —— 技术题答案里的技术参数数字也可能不在简历中，
    最终由用户自行判断是否属实。
    """
    if not answer:
        return []
    resume_text = resume_text or ""
    pairs = set(_number_tokens(resume_text)) | set(_cn_number_tokens(resume_text))
    resume_nums = {num for num, _unit in pairs}
    resume_units: Dict[str, set] = {}
    for num, unit in pairs:
        resume_units.setdefault(num, set()).add(unit)

    suspicious: List[str] = []
    for token in _NUMBER_RE.findall(answer):
        token = token.strip()
        if not token:
            continue
        num, unit = _split_num_unit(token)
        if num not in resume_nums:
            if token not in suspicious:
                suspicious.append(token)
            continue
        known_time = resume_units.get(num, set()) & _TIME_UNITS
        if unit in _TIME_UNITS and known_time and unit not in known_time:
            if token not in suspicious:
                suspicious.append(token)
    return suspicious


def generate_questions_node(state: InterviewState, config: RunnableConfig | None) -> InterviewState:
    """基于简历 + JD + 面经生成面试题"""
    user = (config or {}).get("configurable", {}).get("user")
    if user is None:
        raise ValueError("未找到当前用户")
    logger.info("开始生成面试题")

    prompt = f"""【简历】\n{state['resume_text']}\n\n【目标JD】\n{state['jd_text']}"""

    if state.get("article_content"):
        prompt += f"\n\n【面经参考】\n{state['article_content']}"

    # RAG：检索语料库中最相似的同类岗位面经作为出题参考（失败静默降级）
    # 注意：必须带上 db —— retrieve_similar 在 db 为 None 时直接返回 []（语料可见性
    # 过滤依赖 DB，无法只靠向量库判定归属）。漏传会让这段参考被静默关掉。
    try:
        from app.modules.corpus.services import retrieve_similar
        db = (config or {}).get("configurable", {}).get("db")
        query = state.get("jd_text") or state.get("resume_text") or ""
        similar = retrieve_similar(
            query,
            top_k=2,
            item_type="interview",
            db=db,
            viewer_user_id=getattr(user, "id", None),
            include_private=True,
        )
        if similar:
            refs = "\n\n".join(s["raw_text"][:800] for s in similar)
            prompt += f"\n\n【语料库面经参考】\n{refs}"
    except Exception as e:  # noqa: BLE001
        logger.warning("语料检索失败（不影响出题）: %s", e)

    if state.get("limit", 0) > 0:
        prompt += f"\n\n请生成最多 {state['limit']} 道题目。"

    try:
        llm = get_user_llm_or_raise(user)
        response = llm.invoke([
            SystemMessage(content=SYSTEM_PROMPT),
            HumanMessage(content=prompt),
        ])

        raw_content = response.content
        logger.debug(f"LLM 原始返回: {raw_content[:200]}...")

        cleaned = _clean_json_response(raw_content)
        parsed = json.loads(cleaned)

        raw_questions = parsed.get("questions", []) if isinstance(parsed, dict) else []
        if not isinstance(raw_questions, list):
            raw_questions = []

        # 别名归一：模型写「技术题」/「technical」也算合法（旧版白名单会整条丢弃）
        valid_questions = [q for q in (normalize_question(x) for x in raw_questions) if q]
        dropped = len(raw_questions) - len(valid_questions)
        if dropped:
            logger.warning(
                "面试题格式不合规被丢弃 %d/%d 条（分类或难度不在允许集合内，已别名归一仍认不出）",
                dropped, len(raw_questions),
            )

        # 按 limit 截断
        if state.get("limit", 0) > 0 and len(valid_questions) > state["limit"]:
            valid_questions = valid_questions[:state["limit"]]

        # 对每道题的参考答案做事实保真校验，标记疑似编造的数字
        for q in valid_questions:
            q["suspicious_numbers"] = check_answer_fidelity(
                state["resume_text"], q.get("answer", "")
            )

        logger.info(f"生成 {len(valid_questions)} 道有效题目")
        return {**state, "questions": valid_questions,
                "raw_count": len(raw_questions), "dropped_count": dropped, "error": ""}

    except ValueError as e:
        logger.warning(f"面试题生成失败: {e}")
        return {**state, "questions": [], "error": str(e)}
    except json.JSONDecodeError as e:
        logger.error(f"JSON 解析失败: {e}")
        return {**state, "questions": [], "error": "面试题生成格式异常，请重试"}
    except Exception as e:
        logger.error(f"LLM 调用失败: {e}")
        return {**state, "questions": [], "error": f"生成失败: {str(e)}"}


def create_interview_prep_graph():
    """创建面试题生成工作流"""
    workflow = StateGraph(InterviewState)
    workflow.add_node("generate", generate_questions_node)
    workflow.set_entry_point("generate")
    workflow.add_edge("generate", END)
    return workflow.compile()


interview_prep_graph = create_interview_prep_graph()


async def generate_interview_questions(
    resume_text: str,
    jd_text: str,
    user: User,
    article_content: str = "",
    limit: int = 10,
    db: Optional[Session] = None,
) -> dict:
    """
    执行面试题生成（异步入口）

    Args:
        resume_text: 简历文本
        jd_text: JD 文本
        user: 当前登录用户
        article_content: 面经参考内容（可选）
        limit: 生成题目数量上限
        db: 数据库会话（用于按可见性检索语料；不传则跳过语料参考）

    Returns:
        dict: 包含 questions / raw_count / dropped_count / error 字段
              （dropped_count > 0 表示模型给了题、但分类或难度格式认不出来被丢弃，
               与「模型压根没给题」是两回事，调用方可据此给出不同提示）
    """
    initial = {
        "resume_text": resume_text,
        "jd_text": jd_text,
        "article_content": article_content,
        "limit": limit,
        "questions": [],
        "raw_count": 0,
        "dropped_count": 0,
        "error": ""
    }
    with trace_operation("interview_prep", getattr(user, "id", None)):
        result = await interview_prep_graph.ainvoke(
            initial,
            config={"configurable": {"user": user, "db": db}}
        )
    return result
