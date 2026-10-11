"""简历生成 / 优化缺陷修复（服务端侧，2026-10-09）的判据。

覆盖清单里**服务端**的条目：

  S1  姓名 / 联系方式只信 LLM 输出 → 模型漏写手机号就没有联系方式，
      写错就是错误联系方式，而接口仍返回 success=True
  S2  `added_skills` / `added_certifications` 只赋值、无消费方
      → 「护栏检出了编造」和「编造照样落库」同时成立
  S3  `regenerate_section_variants` 直接 return 候选 → 候选是唯一绕过
      生成 / 逐段重写两道把关、又能经 `save_structured` 直接落库的通道
  P1  `page_fit.match_bullets` 的 `used` 未跨经历共享（违反其自身 docstring 的契约）
  P2  质量重试窗口空档：`passed` 要 ≥75，重试条件却是 <70 ⇒ [70, 75) 没人管

断言写法：**反向对照放在同文件**（`test_*_without_*` 那几条），
只断言"缺陷消失了"而不证明"缺陷本来存在"的用例，在这里不算数。
"""

import asyncio
import contextlib

import pytest

from test_resume_generation import _generate_payload

from app.models.user import User
from app.modules.resume_generation import page_fit as P
from app.modules.resume_generation import services as S
from app.modules.resume_generation.schemas import ResumeGenerateRequest

USER = User(id="default", name="默认用户")


# ---------- S1：姓名 / 联系方式回填 ----------


def _model_payload(**over) -> dict:
    """模拟 LLM 返回的 payload：姓名写错、手机号漏写、邮箱写错。"""
    base = {
        "name": "李四",
        "position": "Python 后端",
        "contact": {"phone": "", "email": "wrong@example.com", "github": ""},
        "summary": "计算机科学与技术专业，具备 Python 后端开发能力。",
        "education": [{"school": "示例大学", "major": "计算机科学与技术",
                       "degree": "本科", "start": "2023-09", "end": "2027-06",
                       "detail": ""}],
        "experiences": [{"type": "project", "name": "OfferFlow 求职系统",
                         "role": "后端开发", "start": "2026-03", "end": "2026-08",
                         "bullets": ["使用 FastAPI 搭建后端服务并完成数据库设计"]}],
        "skills": ["Python"],
        "certifications": [],
        "tips": [],
    }
    base.update(over)
    return base


IDENTITY = {"name": "张三", "phone": "13800138000",
            "email": "zhangsan@example.com", "github": ""}


def _build(identity=None):
    return asyncio.run(S._build_result(
        _model_payload(), "Python 后端", None, "", USER, identity_input=identity))


def test_identity_is_backfilled_from_user_input():
    result = _build(IDENTITY)
    assert result.name == "张三"
    assert result.contact["phone"] == "13800138000"
    assert result.contact["email"] == "zhangsan@example.com"


def test_without_identity_input_the_model_value_is_kept():
    """反向对照：不传身份输入时，模型自报的（错的）姓名 / 邮箱原样生效。

    这条证明上一个用例断言的是**回填**这个动作本身，
    而不是别的地方顺手把值改了。
    """
    result = _build(None)
    assert result.name == "李四"
    assert result.contact["email"] == "wrong@example.com"
    assert result.contact["phone"] == ""


def test_blank_user_field_does_not_erase_anything():
    """用户没填的字段不参与覆盖（是否算编造交给 fidelity 判定）。"""
    result = _build({"name": "张三", "phone": "", "email": "", "github": ""})
    assert result.name == "张三"
    assert result.contact["email"] == "wrong@example.com"


# ---------- S2：字段级保真「检出即处置」 ----------


def _req() -> ResumeGenerateRequest:
    return ResumeGenerateRequest(**_generate_payload())


def _result_with_fabrications():
    result = S.GenerationResult(name="张三", position="Python 后端",
                               contact={"phone": "13900000000", "email": "", "github": ""})
    result.resume_text = "张三"
    result.skills = ["Python", "Kubernetes"]
    result.certifications = ["AWS 认证"]
    result.education = [{"school": "示例大学", "major": "计算机科学与技术",
                         "degree": "本科", "start": "2023-09", "end": "2027-06"}]
    result.experiences = [{"name": "OfferFlow 求职 Agent 系统", "role": "后端开发",
                           "bullets": ["使用 FastAPI 搭建后端服务并完成数据库设计"]}]
    return result


FIDELITY = {
    "passed": False,
    "added_skills": ["Kubernetes"],
    "added_certifications": ["AWS 认证"],
    "added_contacts": ["13900000000"],
    "added_schools": [],
    "added_companies": [],
}


def test_fidelity_detector_stays_pure():
    """`check_generation_fidelity` 必须是**纯检测**：它只报，不改产物。"""
    result = _result_with_fabrications()
    report = S.check_generation_fidelity(_req(), result)
    assert report["added_skills"] == ["Kubernetes"]
    assert report["added_certifications"] == ["AWS 认证"]
    assert "Kubernetes" in result.skills          # 检测阶段不得动产物


def test_fabricated_facts_are_stripped_and_reported():
    result = _result_with_fabrications()
    out = S._strip_fabricated_facts(_req(), result, dict(FIDELITY), "")
    assert out.skills == ["Python"]               # 编造技能被剔除
    assert out.certifications == []               # 编造证书被剔除
    assert "13900000000" not in (out.contact or {}).values()
    assert any("删除" in t for t in out.tips)
    assert out.risks, "剔除后必须留下可追溯的记录（tips + risks）"
    assert out.fidelity.get("removed", {}).get("skills") == ["Kubernetes"]
    assert out.fidelity.get("passed") is True     # 处置后应是干净的最终状态


def test_nothing_removed_returns_unchanged():
    result = _result_with_fabrications()
    clean = {"passed": True, "added_skills": [], "added_certifications": [],
             "added_contacts": [], "added_schools": [], "added_companies": []}
    out = S._strip_fabricated_facts(_req(), result, clean, "")
    assert out.skills == ["Python", "Kubernetes"]
    assert out.certifications == ["AWS 认证"]


# ---------- S3：候选变体的数字净化 ----------


def _structured_with_bullet(text: str) -> dict:
    return {"name": "张三", "position": "后端", "contact": {}, "summary": "",
            "education": [], "skills": [], "certifications": [],
            "experiences": [{"name": "OfferFlow", "role": "后端", "bullets": [text]}]}


def test_candidate_numbers_without_source_are_removed():
    structured = _structured_with_bullet("负责订单系统重构")
    source = S._structured_source_text(structured)
    fixed, removed = S._sanitize_candidate(
        {"bullets": ["负责订单系统重构，响应时间降低 40%，支撑 5000 QPS"]}, source)
    bullet = fixed["bullets"][0]
    assert "40%" not in bullet
    assert "5000" not in bullet
    assert removed
    assert "订单系统重构" in bullet                # 有出处的内容必须保留


def test_candidate_numbers_present_in_source_are_kept():
    structured = _structured_with_bullet("响应时间降低 40%")
    source = S._structured_source_text(structured)
    fixed, removed = S._sanitize_candidate(
        {"bullets": ["响应时间降低 40%"]}, source)
    assert fixed["bullets"] == ["响应时间降低 40%"]
    assert removed == []


def test_candidate_sanitize_recurses_into_lists_and_plain_strings():
    fixed, removed = S._sanitize_candidate(
        ["效率提升 300%", {"nested": ["支持 1200 并发"]}],
        "效率提升 300% 是真实数据")
    assert fixed[0] == "效率提升 300%"             # 有出处 → 不动
    assert "1200" not in str(fixed[1])             # 无出处 → 删除
    assert removed


# ---------- P1：候选池的 used 共享 ----------


def test_match_bullets_reports_unclaimed_candidates_when_used_is_shared():
    """同一份候选池里，两段经历不能各自认领同一行。"""
    candidates = ["负责 A 模块的接口开发与联调，梳理调用链路并补齐异常处理"]
    used = set()
    first = P.match_bullets(["负责 A 模块的接口开发与联调，梳理调用链路并补齐异常处理"],
                            candidates, used)
    second = P.match_bullets(["负责 B 模块的接口开发与联调，梳理调用链路并补齐异常处理"],
                             candidates, used)
    assert first, "第一个应该认领到"
    assert not second, "第二个不能再认领同一行（这正是 used 存在的意义）"


def test_match_bullets_without_used_lets_both_claim():
    """反向对照：不传 used 时，同一行会被两段经历同时认领 —— 缺陷本来就在这里。"""
    candidates = ["负责 A 模块的接口开发与联调，梳理调用链路并补齐异常处理"]
    first = P.match_bullets(["负责 A 模块的接口开发与联调，梳理调用链路并补齐异常处理"],
                            candidates)
    second = P.match_bullets(["负责 B 模块的接口开发与联调，梳理调用链路并补齐异常处理"],
                             candidates)
    assert first and second


def test_candidates_for_returns_pool_key():
    blocks = [("OfferFlow 求职系统", ["块内要点一"]),
              ("另一个项目", ["块内要点二"])]
    key, cands = P._candidates_for({"name": "OfferFlow 求职系统", "role": ""}, blocks, "")
    assert key == 0
    assert cands == ["块内要点一"]


def test_candidates_for_falls_back_to_global_pool_with_marker():
    """块标题对不上但名字还在全文里 → 退化为全池，池标识必须是**同一个**（-1）。"""
    blocks = [("完全无关的标题", ["要点一"])]
    key, cands = P._candidates_for({"name": "OfferFlow", "role": ""},
                                   blocks, "offerflow")
    assert key == -1
    assert cands == ["要点一"]
    assert P._candidates_for({"name": "不存在", "role": ""}, blocks, "") == (None, [])


# ---------- P2：质量重试窗口 ----------


def test_quality_gate_retries_in_the_70_to_75_window(monkeypatch):
    """72 分（>=70 但不 >=75）必须触发重试 —— 这正是此前的空档区间。

    计数口径：`_quality_gate` **只在重试分支里**调 `_generate_payload`
    （首次评分用的是传进来的 result），所以「重试了一次」== 计数 1。
    """
    calls = {"payload": 0}

    async def fake_payload(req, user, jd_prompt, reference, extra_hint="", target_length=800):
        calls["payload"] += 1
        return _model_payload()

    async def fake_build(payload, *args, **kwargs):
        result = S.GenerationResult(name="张三")
        result.resume_text = "一份足够长的简历正文"
        return result

    class _Quality:
        def __init__(self, total):
            self.total_score = total
            self.passed = total >= 75
            self.feedback_hint = "请补足量化结果"

    scores = iter([72, 90])

    def fake_score(result, jd_prompt, direction=""):
        return _Quality(next(scores))

    monkeypatch.setattr(S, "_generate_payload", fake_payload)
    monkeypatch.setattr(S, "_build_result", fake_build)
    monkeypatch.setattr(S, "_score_result_quality", fake_score)
    monkeypatch.setattr(S, "report_to_dict", lambda q: {"total_score": q.total_score})
    monkeypatch.setattr(S, "trace_operation",
                        lambda *a, **k: contextlib.nullcontext())

    result = asyncio.run(S._quality_gate(
        _req(), USER, _result_with_fabrications(), None, "", "", [], []))
    assert calls["payload"] == 1, "70~75 分区间没有触发质量重试"
    assert result.quality.get("total_score") == 90


def test_quality_gate_does_not_retry_when_passed(monkeypatch):
    """反向对照：达标（>=75）就不该再调一次模型。"""
    calls = {"payload": 0}

    async def fake_payload(req, user, jd_prompt, reference, extra_hint="", target_length=800):
        calls["payload"] += 1
        return _model_payload()

    class _Quality:
        total_score = 88
        passed = True
        feedback_hint = ""

    monkeypatch.setattr(S, "_generate_payload", fake_payload)
    monkeypatch.setattr(S, "_score_result_quality", lambda *a, **k: _Quality())
    monkeypatch.setattr(S, "report_to_dict", lambda q: {"total_score": q.total_score})
    monkeypatch.setattr(S, "trace_operation",
                        lambda *a, **k: contextlib.nullcontext())

    asyncio.run(S._quality_gate(
        _req(), USER, _result_with_fabrications(), None, "", "", [], []))
    assert calls["payload"] == 0
