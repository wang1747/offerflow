"""面试题生成 LangGraph 工作流"""

import json
import logging
import re
from typing import TypedDict, List, Optional

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
    error: str


def _clean_json_response(raw: str) -> str:
    """清洗 LLM 输出的 JSON 字符串"""
    raw = re.sub(r"^```json\s*", "", raw.strip())
    raw = re.sub(r"^```\s*", "", raw)
    raw = re.sub(r"```$", "", raw)
    return raw.strip()


def _validate_question(item: dict) -> bool:
    """校验单个题目字段"""
    if not isinstance(item, dict):
        return False
    if not item.get("question") or not isinstance(item["question"], str):
        return False
    category = item.get("category", "")
    if category not in ["技术", "项目", "行为"]:
        return False
    difficulty = item.get("difficulty", "")
    if difficulty not in ["简单", "中等", "困难"]:
        return False
    return True


_NUMBER_RE = re.compile(
    r"\d+(?:\.\d+)?\s*(?:%|万|亿|倍|千|百|GB|MB|KB|TB|ms|s|分钟|小时|天|QPS|TPS|人|个|条|次|元|年|月|日)?"
)


def _number_core(num: str) -> str:
    """取数字核心（去掉单位），用于跨表述比对。"""
    m = re.match(r"\d+(?:\.\d+)?", num)
    return m.group(0) if m else num


def check_answer_fidelity(resume_text: str, answer: str) -> List[str]:
    """校验参考答案里的数字是否都来自简历原文。

    返回疑似「简历中没有」的数字列表（如 AI 自由发挥的「提效40%」）。
    只做标记、不拦截——技术题答案里的技术参数数字也可能不在简历中，
    最终由用户自行判断是否属实。
    """
    if not answer:
        return []
    resume_cores = {_number_core(n) for n in _NUMBER_RE.findall(resume_text or "")}
    suspicious: List[str] = []
    for n in _NUMBER_RE.findall(answer):
        n = n.strip()
        if _number_core(n) not in resume_cores and n not in suspicious:
            suspicious.append(n)
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

        questions = parsed.get("questions", [])
        valid_questions = [q for q in questions if _validate_question(q)]

        # 按 limit 截断
        if state.get("limit", 0) > 0 and len(valid_questions) > state["limit"]:
            valid_questions = valid_questions[:state["limit"]]

        # 对每道题的参考答案做事实保真校验，标记疑似编造的数字
        for q in valid_questions:
            q["suspicious_numbers"] = check_answer_fidelity(
                state["resume_text"], q.get("answer", "")
            )

        logger.info(f"生成 {len(valid_questions)} 道有效题目")
        return {**state, "questions": valid_questions, "error": ""}

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
        dict: 包含 questions 和 error 字段
    """
    initial = {
        "resume_text": resume_text,
        "jd_text": jd_text,
        "article_content": article_content,
        "limit": limit,
        "questions": [],
        "error": ""
    }
    with trace_operation("interview_prep", getattr(user, "id", None)):
        result = await interview_prep_graph.ainvoke(
            initial,
            config={"configurable": {"user": user, "db": db}}
        )
    return result
