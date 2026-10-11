import json
import logging
from typing import TypedDict

from langgraph.graph import StateGraph, END
from langchain_core.messages import SystemMessage, HumanMessage
from langchain_core.runnables import RunnableConfig

from app.core.llm import get_user_llm_or_raise
from app.core.prompt_guards import INJECTION_GUARD, attach
from app.models.user import User
from app.observability.tracing import trace_operation

logger = logging.getLogger(__name__)


def _clean_json_response(raw: str) -> str:
    """清洗 LLM 输出的 JSON 字符串"""
    import re
    raw = re.sub(r"^```json\s*", "", raw.strip())
    raw = re.sub(r"^```\s*", "", raw)
    raw = re.sub(r"```$", "", raw)
    return raw.strip()


class JDState(TypedDict):
    raw_text: str
    parsed: dict
    error: str


def parse_jd_node(state: JDState, config: RunnableConfig | None) -> JDState:
    """Parse JD text into structured JSON"""
    user = (config or {}).get("configurable", {}).get("user")
    if user is None:
        raise ValueError("未找到当前用户")
    raw_text = state["raw_text"]
    # 输入是第三方提供的 JD 原文 ⇒ 挂防注入（本函数只解析、不打分、不产出简历正文）。
    system_prompt = attach(
        "你是专业的 JD（职位描述）解析器。请从 JD 原文中提取结构化信息。\n"
        "核心原则：忠实原文，禁止编造——只提取 JD 原文中明确出现的信息，原文没有的一律留空，绝不凭空补全或猜测。\n"
        "输出严格 JSON：\n"
        '{\n'
        '    "company": "公司名",\n'
        '    "position": "职位名",\n'
        '    "must_have": ["必备要求"],\n'
        '    "nice_to_have": ["加分项"],\n'
        '    "tech_stack": {"backend": [], "frontend": [], "infra": [], "other": []},\n'
        '    "hidden_signals": ["隐藏信号"]\n'
        '}\n'
        "规则：\n"
        "1. company / position：只在原文明确写出时提取，没有就留空字符串，禁止猜测或补全公司名、职位名。\n"
        "2. must_have / nice_to_have：只提取原文明确列出的要求，不得自行添加原文没有的技能或条件。\n"
        "3. tech_stack：只归类原文出现过的技术名词，不得凭空添加。\n"
        "4. hidden_signals：可基于原文措辞推断（如「优先」暗示看重某项能力），但不得编造原文没有的事实。\n"
        "5. 无法提取的字段用空字符串或空列表。",
        INJECTION_GUARD,
    )
    try:
        # 获取用户配置的 LLM
        llm = get_user_llm_or_raise(user)

        response = llm.invoke([
            SystemMessage(content=system_prompt),
            HumanMessage(content=raw_text),
        ])
        parsed = json.loads(_clean_json_response(response.content))
        return {"raw_text": state["raw_text"], "parsed": parsed, "error": ""}
    except ValueError as e:
        # 用户未配置 LLM
        logger.warning(f"JD 解析失败: {e}")
        return {"raw_text": state["raw_text"], "parsed": {}, "error": str(e)}
    except Exception as e:
        logger.error(f"JD 解析异常: {e}")
        return {"raw_text": state["raw_text"], "parsed": {}, "error": str(e)}


def create_jd_analysis_graph():
    workflow = StateGraph(JDState)
    workflow.add_node("parse", parse_jd_node)
    workflow.set_entry_point("parse")
    workflow.add_edge("parse", END)
    return workflow.compile()


jd_analysis_graph = create_jd_analysis_graph()


async def analyze_jd(raw_text: str, user: User) -> dict:
    """Execute JD analysis with user-specific LLM config"""
    initial_state = {"raw_text": raw_text, "parsed": {}, "error": ""}
    with trace_operation("jd_parse", getattr(user, "id", None)):
        result = await jd_analysis_graph.ainvoke(initial_state, config={"configurable": {"user": user}})
    return result
