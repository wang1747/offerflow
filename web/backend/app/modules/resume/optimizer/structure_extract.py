"""把优化后的简历纯文本解析成结构化数据（供导出保留层次）。

优化模块输出的是纯文本（optimized_text），导出时为了保留「标题行 vs bullet 行」
的层次，用 LLM 把纯文本再解析成结构化（字段结构与生成模块 generation 一致）。

提取失败返回 None，由调用方回退纯文本导出路径，不阻塞优化主流程。
"""

import json
import logging
import re
from typing import Optional

from langchain_core.messages import HumanMessage, SystemMessage

from app.core.llm import get_user_llm_or_raise
from app.core.exceptions import QuotaExceededError
from app.core.prompt_guards import INJECTION_GUARD, attach
from app.models.user import User
from app.observability.tracing import trace_operation

logger = logging.getLogger(__name__)

EXTRACT_SYSTEM_PROMPT = """你是简历解析专家。把下面的简历纯文本解析成结构化 JSON。

只提取简历里已有的信息，不要编造、不要补充任何原文没有的内容。

输出结构：
{
  "name": "姓名",
  "position": "求职意向（若无则空字符串）",
  "contact": {"phone": "电话", "email": "邮箱", "github": "链接"},
  "summary": "个人总结段落（若无则空字符串）",
  "education": [
    {"school": "学校", "major": "专业", "degree": "学历", "start": "开始时间", "end": "结束时间", "detail": "主修课程/绩点/荣誉等子行，合并为一句，无则空字符串"}
  ],
  "experiences": [
    {"type": "project/internship/work", "name": "项目或公司名", "role": "角色", "start": "开始时间", "end": "结束时间", "bullets": ["要点1", "要点2"]}
  ],
  "skills": ["技能"],
  "certifications": ["证书"]
}

解析规则：
1. experiences 的 type 按分节标题判断：项目经历→project、实习经历→internship、工作经历→work，其他→project。
2. bullets 是每条要点的纯文本（去掉前面的 - 或 • 符号）。
3. education 的 detail 把「主修课程/绩点/荣誉」等子行合并成一句，用「；」分隔。
4. 时间只保留原文写的内容，不要补全。
5. 没有的字段用空字符串或空数组。

只输出 JSON，不要任何其他文字。"""

# 输入是（可能被注入的）简历纯文本 ⇒ 挂防注入。
# 它只做解析、不打分、不产出新的简历正文 ⇒ 不需要占位符与禁给分条款。
EXTRACT_SYSTEM_PROMPT = attach(EXTRACT_SYSTEM_PROMPT, INJECTION_GUARD)


def _clean_json(raw: str) -> str:
    text = (raw or "").strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"```\s*$", "", text)
    return text.strip()


def extract_structured(resume_text: str, user: User) -> Optional[dict]:
    """用 LLM 把纯文本简历解析成结构化数据，失败返回 None。"""
    try:
        llm = get_user_llm_or_raise(user)
    except QuotaExceededError:
        raise
    except ValueError as e:
        logger.warning("结构化提取失败（未配置模型）: %s", e)
        return None

    try:
        with trace_operation("resume_optimize_extract", getattr(user, "id", None)):
            response = llm.invoke([
                SystemMessage(content=EXTRACT_SYSTEM_PROMPT),
                HumanMessage(content=f"简历内容：\n{resume_text}"),
            ])
    except Exception as e:
        logger.error("结构化提取 LLM 异常: %s", e)
        return None

    text = _clean_json(response.content)
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        logger.warning("结构化提取：LLM 返回非 JSON")
        return None
    try:
        data = json.loads(text[start:end + 1])
    except json.JSONDecodeError as e:
        logger.warning("结构化提取：JSON 解析失败 %s", e)
        return None

    if not isinstance(data, dict) or not data.get("name"):
        return None
    return data
