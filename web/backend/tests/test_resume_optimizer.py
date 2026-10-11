import asyncio
import json

from langchain_core.messages import AIMessage

from app.models.user import User
from app.modules.resume.optimizer.contracts import JDRequirement
from app.modules.resume.optimizer.sections import (
    preservation_result,
    split_resume_blocks,
)


def test_split_resume_blocks_with_headings():
    text = "姓名：张三\n教育经历\n示例大学\n项目经历\nOfferFlow 求职 Agent 系统"
    blocks = split_resume_blocks(text)
    assert len(blocks) == 3
    assert "示例大学" in blocks[1]


def test_preservation_rejects_fabricated():
    original = "示例大学，计算机科学与技术，2027届"
    fabricated = "毕业于北京大学，计算机专业，2028届"
    result = preservation_result(original, fabricated)
    assert result.passed is False


def test_preservation_accepts_original():
    original = "示例大学，计算机科学与技术，2027届"
    result = preservation_result(original, original)
    assert result.passed is True
    assert result.score == 1.0


def test_preservation_accepts_legacy_glued_contacts():
    original = (
        "电话：18733444491 邮箱：18733444491@163.comGitHub："
        "https://github.com/wang1747"
    )
    rewritten = (
        "电话：18733444491\n邮箱：18733444491@163.com\n"
        "GitHub：https://github.com/wang1747"
    )
    result = preservation_result(original, rewritten)
    assert result.passed is True


def test_preservation_accepts_legitimate_paraphrase():
    original = (
        "负责设计并开发高并发后端服务，使用 Python、FastAPI、PostgreSQL，服务十万用户。"
    )
    rewritten = (
        "基于 Python、FastAPI 与 PostgreSQL 设计高并发后端服务，支撑十万级用户访问。"
    )
    assert preservation_result(original, rewritten).passed is True


def test_preservation_rejects_large_content_drop():
    original = (
        "项目经历\\n负责设计并开发高并发后端服务，使用 Python、FastAPI、PostgreSQL，"
        "服务十万用户。\\n教育经历\\n示例大学 2027届"
    )
    rewritten = "项目经历\\n负责开发后端服务。"
    assert preservation_result(original, rewritten).passed is False


def test_preservation_rejects_newly_added_facts():
    original = "示例大学，Python 后端开发，2027届"
    fabricated = (
        "示例大学，Python 后端开发，2027届\n"
        "曾任字节跳动架构师，熟悉 Rust、Kubernetes，性能提升 40%"
    )
    result = preservation_result(original, fabricated)
    assert result.passed is False
    assert result.added_facts
    assert any("Rust" in item or "Kubernetes" in item for item in result.added_facts)


def _jd() -> JDRequirement:
    return JDRequirement(
        raw_text="Python 后端实习，要求熟悉 FastAPI 和 Docker",
        company="某公司",
        position="后端实习生",
        must_have=["Python", "FastAPI"],
        nice_to_have=["Docker"],
    )


class ExceptionLLM:
    def invoke(self, messages):
        raise RuntimeError("llm unavailable")


def test_optimizer_falls_back_when_llm_unavailable(monkeypatch):
    import app.modules.resume.optimizer.service as service

    original = "示例大学，计算机科学与技术，2027届"
    monkeypatch.setattr(service, "get_user_llm_or_raise", lambda user: ExceptionLLM())

    result = asyncio.run(
        service.optimize_resume_text(original, _jd(), User(id="u", name="u"))
    )
    assert result.optimized_text == original
    assert result.preservation.fallback is True
    assert result.error  # 现在会明确报错，而不是静默保存


class FabricatedLLM:
    """返回把学校改成北大的伪造结果。"""

    def invoke(self, messages):
        payload = {
            "optimized_text": "毕业于北京大学，计算机专业，2028届",
            "changes": [],
            "added_keywords": [],
            "removed_keywords": [],
        }
        return AIMessage(content=json.dumps(payload, ensure_ascii=False))


def test_optimizer_rejects_fabricated_rewrite(monkeypatch):
    import app.modules.resume.optimizer.service as service

    original = "示例大学，计算机科学与技术，2027届"
    monkeypatch.setattr(service, "get_user_llm_or_raise", lambda user: FabricatedLLM())

    result = asyncio.run(
        service.optimize_resume_text(original, _jd(), User(id="u", name="u"))
    )
    assert result.optimized_text == original
    assert "北京大学" not in result.optimized_text


class RewriteLLM:
    """返回一份合法改写（保留事实 + 对齐关键词）。"""

    def __init__(self, original: str):
        self.original = original

    def invoke(self, messages):
        payload = {
            "optimized_text": (
                self.original
                + "\n技能：Python、FastAPI、Docker，熟悉高并发后端开发"
            ),
            "changes": [
                {
                    "section": "技能",
                    "before": "计算机科学与技术",
                    "after": "计算机科学与技术，Python、FastAPI",
                    "reason": "对齐 JD 必备技能",
                }
            ],
            "added_keywords": ["Python", "FastAPI"],
            "removed_keywords": [],
        }
        return AIMessage(content=json.dumps(payload, ensure_ascii=False))


def test_optimizer_applies_rewrite(monkeypatch):
    import app.modules.resume.optimizer.service as service

    original = (
        "示例大学，计算机科学与技术，2027届\n"
        "技能：Python、FastAPI、Docker、高并发后端开发"
    )
    monkeypatch.setattr(
        service, "get_user_llm_or_raise", lambda user: RewriteLLM(original)
    )

    result = asyncio.run(
        service.optimize_resume_text(original, _jd(), User(id="u", name="u"))
    )
    assert result.preservation.passed is True
    assert "Python" in result.optimized_text
    assert result.edit_count == 1
    assert result.changes[0].section == "技能"
    assert result.added_keywords == ["Python", "FastAPI"]


def test_optimizer_gap_analysis_filled(monkeypatch):
    import app.modules.resume.optimizer.service as service

    original = "示例大学，计算机科学与技术，2027届"
    class NoopLLM:
        def invoke(self, messages):
            return AIMessage(content=json.dumps({
                "optimized_text": original,
                "changes": [],
                "added_keywords": [],
                "removed_keywords": [],
            }, ensure_ascii=False))

    monkeypatch.setattr(
        service, "get_user_llm_or_raise", lambda user: NoopLLM()
    )

    result = asyncio.run(
        service.optimize_resume_text(original, _jd(), User(id="u", name="u"))
    )
    # 差距分析应识别出原文缺少 Python/FastAPI/Docker
    assert "Python" in result.gap.missing
    assert "FastAPI" in result.gap.missing


def test_optimizer_errors_on_empty_resume(monkeypatch):
    import app.modules.resume.optimizer.service as service

    monkeypatch.setattr(
        service, "get_user_llm_or_raise", lambda user: RewriteLLM("")
    )
    result = asyncio.run(
        service.optimize_resume_text("   ", _jd(), User(id="u", name="u"))
    )
    assert result.error
    assert result.optimized_text == ""


class OverlongLLM:
    """第一次返回超长结果，第二次返回精简结果（用于测试一页硬约束）。"""

    def __init__(self, condensed: str):
        self.condensed = condensed
        self.calls = 0

    def invoke(self, messages):
        self.calls += 1
        if self.calls == 1:
            long_text = (
                "张三 13800138000 wang@qq.com\n示例大学 2027届\n"
                "项目经历\n"
                + "\n".join(
                    f"项目{chr(0x4e00 + i)}：做了一件很重要的高并发后端开发工作" * 3
                    for i in range(15)
                )
            )
            payload = {"optimized_text": long_text, "changes": [], "added_keywords": []}
        else:
            payload = {"optimized_text": self.condensed, "changes": []}
        return AIMessage(content=json.dumps(payload, ensure_ascii=False))


def test_optimizer_condenses_overlong_result(monkeypatch):
    """超长优化结果应触发精简，压缩到一页内。"""
    import app.modules.resume.optimizer.service as service

    original = "张三 13800138000 wang@qq.com\n示例大学 2027届\nPython 后端开发"
    condensed = original + "\n补充说明：负责后端接口开发"
    llm = OverlongLLM(condensed)
    monkeypatch.setattr(service, "get_user_llm_or_raise", lambda user: llm)

    result = asyncio.run(service.optimize_resume_text(original, _jd(), User(id="u", name="u")))
    assert result.error == ""
    assert "补充说明" in result.optimized_text


def test_optimizer_keeps_result_when_still_overlong(monkeypatch):
    """精简后仍超长，应保留精简成果并给出提示，而不是粗暴回退原简历。"""
    import app.modules.resume.optimizer.service as service

    original = "张三 13800138000 wang@qq.com\n示例大学 2027届"
    long_text = "张三 13800138000 wang@qq.com\n项目经历\n" + "\n".join(
        f"项目{chr(0x4e00 + i)}：做了一件很重要的高并发后端开发工作" * 3
        for i in range(15)
    )
    llm = OverlongLLM(long_text)
    monkeypatch.setattr(service, "get_user_llm_or_raise", lambda user: llm)

    result = asyncio.run(service.optimize_resume_text(original, _jd(), User(id="u", name="u")))
    assert result.error == ""
    assert result.optimized_text  # 仍返回了内容，不是空
    assert result.length_warning  # 给出超长提示
    assert result.preservation.fallback is False  # 不是回退

