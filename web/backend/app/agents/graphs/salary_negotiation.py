"""薪资谈判 LLM 调用：HR 压价角色 + 教练反馈 + 总结。"""
import logging
import re
from typing import List

from langchain_core.messages import SystemMessage, HumanMessage

from app.core.llm import get_user_llm_or_raise
from app.core.prompt_guards import INJECTION_GUARD, attach
from app.models.user import User
from app.observability.tracing import trace_operation

logger = logging.getLogger(__name__)

SCENARIO_LABELS = {
    "offer": "初次谈薪（刚收到 offer，第一次谈薪资）",
    "counter": "应对压价（HR 以预算/市场行情等理由压低你的期望）",
    "raise": "争取涨幅（内部调薪或升职谈判）",
    "final": "应对「这是最终报价」（HR 声称不能再加）",
}

HR_SYSTEM_PROMPT = """你是公司的 HR / 招聘经理，正在和候选人进行薪资谈判。你要扮演一个真实、会压价的 HR，而不是轻易答应。

当前场景：{scenario}

你可以灵活使用这些压价手段：
- 预算有限：「这个岗位的预算就这么多」
- 市场行情：「按市场行情，你这个经验就是这个价」
- 经验不足：「你经验还浅，这个薪资已经很有诚意了」
- 最终报价：「这是我们的最终报价，不能再加了」
- 拖延：「我需要再跟上面申请一下」

谈判要求：
1. 每次只回一句话，简短、口语化，像真人 HR 在电话/微信里说的
2. 要有真实的推回和施压，不要轻易让步
3. 不要一次性把话说死，给候选人留出继续谈的空间
4. 偶尔表达犹豫或「这超出预算了」，制造真实感
5. 只输出 HR 说的话，不要任何其他内容"""

COACH_SYSTEM_PROMPT = """你是资深薪资谈判教练。候选人刚和 HR 进行了一轮对话，请你点评候选人的回应。

HR 说的：{hr_message}
候选人回应：{user_message}
候选人设定：开口报价 {target}，底线 {bottom}
（说明：开口报价是候选人往高了报的锚点，可以适当让步；真正要守住的是底线，尽量争取落在开口报价和底线之间偏上的位置）
业绩证明点：{context}

点评要求（直接、犀利、可执行）：
1. 优点：一句话
2. 问题：重点指出候选人是否「接受了对方的框架」——比如一开口就「我理解预算有限」，等于默认了对方的前提，这是最致命的错误
3. 更好的说法：给一句具体可替代的话术（用「价值贡献」回应，而非「个人需要」）

输出简短有力，别啰嗦，直接给可用的反击话术。"""

SUMMARY_SYSTEM_PROMPT = """你是薪资谈判教练。根据整场谈判对话，给出总结。

要求：
1. 总体表现（一句话）
2. 亮点（1-2 个）
3. 最需要改进的地方（1-2 个）
4. 下次谈判的核心建议（一句话）

只输出总结内容，不要其他文字。"""

# 三条都含用户可控文本（HR 的台词、候选人的回应、整场对话记录）⇒ 都挂防注入。
# ⚠️ HR / COACH 两条之后会走 str.format(...)，所以共用条款文本**必须零花括号** ——
# 一旦条款里出现 `{`，format 会抛 KeyError 把整条链路炸掉。
# 判据：tests/test_prompt_guards.py 断言所有条款常量不含花括号。
HR_SYSTEM_PROMPT = attach(HR_SYSTEM_PROMPT, INJECTION_GUARD)
COACH_SYSTEM_PROMPT = attach(COACH_SYSTEM_PROMPT, INJECTION_GUARD)
SUMMARY_SYSTEM_PROMPT = attach(SUMMARY_SYSTEM_PROMPT, INJECTION_GUARD)


def _clean_text(text: str) -> str:
    if not text:
        return ""
    text = re.sub(r"^```json\s*", "", text.strip())
    text = re.sub(r"^```\s*", "", text)
    text = re.sub(r"```$", "", text)
    return text.strip()


def _role_name(msg: dict) -> str:
    return "HR" if msg.get("role") == "hr" else "候选人"


def generate_hr_message(
    scenario: str,
    target: str,
    bottom: str,
    context: str,
    history: List[dict],
    user: User,
) -> str:
    """生成 HR 的下一句话：开场白（history 为空）或后续压价。"""
    scenario_label = SCENARIO_LABELS.get(scenario, SCENARIO_LABELS["offer"])
    system = HR_SYSTEM_PROMPT.format(scenario=scenario_label)

    parts = []
    if target:
        parts.append(f"候选人报的期望薪资（开口价）：{target}")
    if bottom:
        parts.append(f"候选人的底线薪资：{bottom}")
    if context:
        parts.append(f"候选人的业绩证明点：{context}")

    if history:
        parts.append("\n对话历史：")
        for m in history:
            parts.append(f"{_role_name(m)}：{m.get('content', '')}")
        parts.append("\n请根据上面的对话，继续以 HR 身份回应（继续压价 / 推进谈判）。")
    else:
        parts.append("\n请以 HR 身份开始这场薪资谈判，说出你的第一句话（结合场景自然开场）。")

    human = "\n".join(parts)
    try:
        llm = get_user_llm_or_raise(user)
        with trace_operation("negotiation_hr", getattr(user, "id", None)):
            response = llm.invoke([SystemMessage(content=system), HumanMessage(content=human)])
        return _clean_text(response.content)
    except ValueError as e:
        logger.warning("生成 HR 消息失败: %s", e)
        return str(e)
    except Exception as e:
        logger.error("生成 HR 消息异常: %s", e)
        return "关于薪资，我们给出的范围是行业平均水平，你可以说说你的想法。"


def coach_answer(
    hr_message: str,
    user_message: str,
    target: str,
    bottom: str,
    context: str,
    user: User,
) -> str:
    """对候选人的回应给出谈判教练点评。"""
    system = COACH_SYSTEM_PROMPT.format(
        hr_message=hr_message,
        user_message=user_message,
        target=target or "未设定",
        bottom=bottom or "未设定",
        context=context or "未填写",
    )
    human = "请点评候选人的这一轮回应。"
    try:
        llm = get_user_llm_or_raise(user)
        with trace_operation("negotiation_coach", getattr(user, "id", None)):
            response = llm.invoke([SystemMessage(content=system), HumanMessage(content=human)])
        return _clean_text(response.content)
    except ValueError as e:
        logger.warning("教练点评失败: %s", e)
        return str(e)
    except Exception as e:
        logger.error("教练点评异常: %s", e)
        return "这一轮回应可以再想想怎么用「价值」来回应对方的压价。"


def generate_negotiation_summary(messages: List[dict], user: User) -> str:
    """生成整场谈判总结。"""
    if not messages:
        return ""
    conversation = "\n".join(
        f"{_role_name(m)}：{m.get('content', '')}" for m in messages
    )
    human = "整场谈判对话：\n" + conversation
    try:
        llm = get_user_llm_or_raise(user)
        with trace_operation("negotiation_summary", getattr(user, "id", None)):
            response = llm.invoke([SystemMessage(content=SUMMARY_SYSTEM_PROMPT), HumanMessage(content=human)])
        return _clean_text(response.content)
    except ValueError as e:
        logger.warning("生成谈判总结失败: %s", e)
        return str(e)
    except Exception as e:
        logger.error("生成谈判总结异常: %s", e)
        return "谈判已结束，请查看逐轮教练点评。"
