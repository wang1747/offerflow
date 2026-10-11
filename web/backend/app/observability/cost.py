"""成本计算：按模型费率表估算每次 LLM 调用的费用。

费率表沿用模型厂商官方标价（**美元 / 1M token**），因此 `calculate_cost` 与
`Trace.total_cost` / `Span.cost` 存的都是美元原值 —— 保持美元存储是为了能和
模型厂商账单逐笔对齐。

面向用户的展示统一走 `usd_to_cny` 折算成人民币，见下方 USD_TO_CNY。
"""

from __future__ import annotations

import os
from typing import Optional

# 模型费率表（美元 / 1M token），input 为输入、output 为输出
RATES: dict[str, dict[str, float]] = {
    # DeepSeek（V4.1-Flash 官方名 deepseek-flash；旧名 deepseek-chat / deepseek-v4-flash 已路由至 Flash）
    "deepseek-flash": {"input": 0.30, "output": 1.20},
    # 旧名保留，便于历史记录按原价回溯
    "deepseek-chat": {"input": 0.27, "output": 1.10},
    "deepseek-reasoner": {"input": 0.55, "output": 2.19},
    "deepseek-v4-pro": {"input": 1.32, "output": 3.96},
    # OpenAI
    "gpt-4o": {"input": 2.50, "output": 10.00},
    "gpt-4o-mini": {"input": 0.15, "output": 0.60},
    "gpt-4.1": {"input": 2.00, "output": 8.00},
    "gpt-4.1-mini": {"input": 0.40, "output": 1.60},
    "gpt-4.1-nano": {"input": 0.10, "output": 0.40},
    "gpt-4-turbo": {"input": 10.00, "output": 30.00},
    "gpt-4": {"input": 30.00, "output": 60.00},
    "gpt-3.5-turbo": {"input": 0.50, "output": 1.50},
    # Moonshot / Kimi
    "moonshot-v1-8k": {"input": 0.15, "output": 0.60},
    "moonshot-v1-32k": {"input": 0.24, "output": 0.96},
    "moonshot-v1-128k": {"input": 0.60, "output": 1.20},
    "kimi-k2": {"input": 0.60, "output": 1.20},
    "kimi-latest": {"input": 0.60, "output": 1.20},
}


# 美元 → 人民币参考汇率。
# 模型厂商按美元标价，人民币金额由展示层按此汇率折算。
# 汇率波动时改环境变量即可，无需改代码：USD_TO_CNY=7.15
USD_TO_CNY: float = float(os.getenv("USD_TO_CNY", "7.2"))


def usd_to_cny(usd: Optional[float]) -> Optional[float]:
    """美元金额 → 人民币（四舍五入到 6 位；None 透传，表示无法估算）"""
    if usd is None:
        return None
    return round(usd * USD_TO_CNY, 6)


def _match_rate(model: str) -> Optional[dict[str, float]]:
    """精确匹配 → 前缀匹配；未命中返回 None（绝不静默回退）"""
    if not model:
        return None
    if model in RATES:
        return RATES[model]
    # 前缀匹配（例如 gpt-4o-2024-08-06 匹配 gpt-4o）
    for key, rate in RATES.items():
        if model.startswith(key):
            return rate
    return None


def calculate_cost(model: Optional[str], prompt_tokens: int, completion_tokens: int) -> Optional[float]:
    """计算单次 LLM 调用费用；未知模型返回 None（表示无法估算）"""
    rate = _match_rate(model or "")
    if rate is None:
        return None
    input_cost = (prompt_tokens / 1_000_000) * rate["input"]
    output_cost = (completion_tokens / 1_000_000) * rate["output"]
    return round(input_cost + output_cost, 6)


def has_known_rate(model: Optional[str]) -> bool:
    return _match_rate(model or "") is not None
