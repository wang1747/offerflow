"""薪资基准数据刷新：让 LLM 基于最新市场行情输出微调后的基准数据。

用途：定期 / 手动刷新落库的基准数据，避免一两年后过时。
安全策略：
- 要求 LLM 保持 key 结构不变，单值变化限定 ±30%，防止编造离谱数字；
- 返回结果经 `salary_benchmark.sanitize_data` 二次校验才落库；
- source 明确标注「模型估算」，与权威调研数据区分可信度。
"""

import json
import logging
import re
from typing import Dict

from langchain_core.messages import HumanMessage, SystemMessage

from app.core.llm import get_llm
from app.observability.tracing import trace_operation

logger = logging.getLogger(__name__)

REFRESH_SYSTEM_PROMPT = """你是薪酬市场分析师。下面是当前使用的「应届生起薪市场基准数据」，请根据你对最新市场行情的了解，输出一份更新后的基准数据。

当前数据（JSON）：
{current_data}

要求：
1. 保持完全相同的 key 结构（degree_base / direction_coef / city_tiers），只调整数值，不要增删 key
2. 只做合理的小幅调整，单个数值相对当前值的变化幅度不超过 ±30%，不要凭空翻倍或砍半
3. degree_base 的值是 [下限, 上限] 两个整数（税前月薪，元）；direction_coef / city_tiers 的值是 [系数, 中文标签]，系数在 0.3~3.0 之间
4. 同时给出 data_year（如 "2027"）和 note（一句话说明主要调整点）

只输出 JSON，不要任何其他文字，格式：
{{"data_year":"2027","note":"...","degree_base":{{...}},"direction_coef":{{...}},"city_tiers":{{...}}}}"""


def _clean_json(raw: str) -> str:
    text = (raw or "").strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"```\s*$", "", text)
    return text.strip()


def generate_updated_benchmark(current_data: Dict) -> Dict:
    """调用 LLM 生成更新后的基准数据。失败时返回空 dict（调用方保持旧数据）。"""
    try:
        llm = get_llm()
    except ValueError as e:
        logger.warning("刷新基准数据失败（服务器未配置默认 LLM）: %s", e)
        return {}

    system = REFRESH_SYSTEM_PROMPT.format(
        current_data=json.dumps(current_data, ensure_ascii=False)
    )
    try:
        with trace_operation("salary_benchmark_refresh", None):
            response = llm.invoke([SystemMessage(content=system), HumanMessage(content="请输出更新后的基准数据。")])
    except Exception as e:
        logger.error("刷新基准数据 LLM 调用异常: %s", e)
        return {}

    text = _clean_json(response.content)
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        logger.warning("刷新基准数据：LLM 返回非 JSON，忽略。内容前 200 字：%s", text[:200])
        return {}
    try:
        return json.loads(text[start:end + 1])
    except json.JSONDecodeError as e:
        logger.warning("刷新基准数据：JSON 解析失败 %s", e)
        return {}
