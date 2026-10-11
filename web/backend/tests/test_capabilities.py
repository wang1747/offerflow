"""领域只读 capabilities 层单测。

对应设计文档 `docs/agent/v0-agent-capability-boundary.md` 第 22 节验收要求，
重点覆盖**越权与信息泄露**——这是 Agent 方案最容易翻车的地方。

覆盖：
1. 跨用户访问必须 not_found（不得泄露"存在但无权限"）。
2. 空数据返回稳定结构，不抛异常。
3. 不返回完整正文（只有截断 preview）。
4. 版本 / 引用字段存在且可用于冲突判断。
5. match.get 不触发重复计算。
"""
import pytest

from app.models.jd import JobDescription
from app.models.match import MatchResult
from app.models.resume import Resume
from app.modules.jd.capabilities import (
    get_jd_capability,
    list_jds_capability,
)
from app.modules.match.capabilities import get_match_capability
from app.modules.resume.capabilities import (
    get_resume_capability,
    list_resumes_capability,
)

OTHER = "other-user"


@pytest.fixture
def owned_jd(db_session):
    jd = JobDescription(
        id="jd-own",
        user_id="default",
        raw_text="Python 后端开发，需要 Redis 与高并发经验。负责核心服务设计。",
        company="某某科技",
        position="后端工程师",
        must_have=["Python", "Redis"],
        nice_to_have=["Kubernetes"],
        tech_stack=["Python", "Redis", "Docker"],
    )
    db_session.add(jd)
    db_session.commit()
    return jd


@pytest.fixture
def other_jd(db_session):
    """另一个用户的 JD——用于验证越权不可读。"""
    jd = JobDescription(
        id="jd-other",
        user_id=OTHER,
        raw_text="机密岗位：Rust 基础建设。",
        company="机密公司",
        position="Rust 工程师",
        must_have=["Rust"],
    )
    db_session.add(jd)
    db_session.commit()
    return jd


@pytest.fixture
def owned_resume(db_session):
    resume = Resume(
        id="resume-own",
        user_id="default",
        version=1,
        raw_text=(
            "张三\n后端开发工程师\n"
            "熟悉 Python、Redis、Docker，有高并发项目经验。\n"
            + "项目经历详述：" + "在多个大型系统中负责核心服务的架构设计与性能优化。" * 20
        ),
        parsed_json={
            "generation": {
                "name": "张三",
                "skills": ["Python", "Redis", "Docker"],
                "experiences": [{"company": "某互联网公司"}],
            }
        },
    )
    db_session.add(resume)
    db_session.commit()
    return resume


@pytest.fixture
def other_resume(db_session):
    resume = Resume(
        id="resume-other",
        user_id=OTHER,
        version=1,
        raw_text="李四\n机密简历内容不应泄露。",
    )
    db_session.add(resume)
    db_session.commit()
    return resume


# ---------------------------------------------------------------------------
# 越权：跨用户必须 not_found
# ---------------------------------------------------------------------------
def test_get_resume_cross_user_blocked(db_session, other_resume):
    result = get_resume_capability(db_session, user_id="default", resume_id="resume-other")
    assert result["status"] == "not_found"
    # 关键：不得泄露任何他人简历内容
    assert "机密" not in str(result)


def test_get_jd_cross_user_blocked(db_session, other_jd):
    result = get_jd_capability(db_session, user_id="default", jd_id="jd-other")
    assert result["status"] == "not_found"
    assert "机密" not in str(result)
    assert "Rust" not in str(result)


def test_get_match_cross_user_blocked(db_session, other_jd, other_resume, owned_jd, owned_resume):
    """用别人的 JD + 别人的简历建匹配，default 读不到。"""
    match = MatchResult(
        jd_id="jd-other",
        resume_id="resume-other",
        score=99.0,
        skill_match_detail={"matched": ["Rust"], "missing": [], "partial": []},
    )
    db_session.add(match)
    db_session.commit()

    result = get_match_capability(
        db_session, user_id="default",
        resume_id="resume-other", jd_id="jd-other",
    )
    assert result["status"] == "not_found"


def test_match_cross_user_jd_with_own_resume_blocked(db_session, other_jd, owned_resume):
    """混合场景：别人的 JD + 我的简历 —— 必须拒绝（防止越权拼接）。"""
    match = MatchResult(
        jd_id="jd-other",
        resume_id="resume-own",
        score=95.0,
        skill_match_detail={"matched": ["Rust"], "missing": [], "partial": []},
    )
    db_session.add(match)
    db_session.commit()

    result = get_match_capability(
        db_session, user_id="default",
        resume_id="resume-own", jd_id="jd-other",
    )
    assert result["status"] == "not_found"
    assert "Rust" not in str(result)


# ---------------------------------------------------------------------------
# 正常路径
# ---------------------------------------------------------------------------
def test_list_resumes_returns_summary_only(db_session, owned_resume):
    result = list_resumes_capability(db_session, user_id="default")
    assert result["total"] == 1
    item = result["items"][0]
    assert item["id"] == "resume-own"
    assert item["summary"]["name"] == "张三"
    # 不得返回完整正文
    assert "raw_text" not in item
    # 正文足够长时必须触发截断，且预览不等于全文
    assert len(owned_resume.raw_text) > 160
    assert len(item["preview"]) < len(owned_resume.raw_text)
    assert item["preview"].endswith("…")


def test_list_resumes_hides_other_users(db_session, owned_resume, other_resume):
    """列表不得包含其他用户的简历。"""
    result = list_resumes_capability(db_session, user_id="default")
    ids = [i["id"] for i in result["items"]]
    assert "resume-other" not in ids


def test_list_resumes_empty_is_stable(db_session):
    """空数据必须返回稳定结构，不抛异常。"""
    result = list_resumes_capability(db_session, user_id="default")
    assert result == {"items": [], "next_cursor": None, "total": 0}


def test_get_resume_returns_version_and_refs(db_session, owned_resume):
    result = get_resume_capability(db_session, user_id="default", resume_id="resume-own")
    assert result["status"] == "succeeded"
    resume = result["resume"]
    assert resume["version"] == 1
    assert resume["document_id"]
    assert "raw_text" not in resume
    # object_version 需可用于冲突判断
    assert "v1" in result["object_version"]
    assert result["object_refs"][0]["type"] == "resume"


def test_list_jds_marks_unparsed(db_session):
    """未解析的 JD 必须能被识别，供 Agent 返回 needs_input。"""
    jd = JobDescription(
        id="jd-bare", user_id="default", raw_text="随便一个岗位描述",
        company="某公司", position="某职位",
    )
    db_session.add(jd)
    db_session.commit()

    result = list_jds_capability(db_session, user_id="default")
    item = result["items"][0]
    assert item["has_parsed"] is False


def test_get_jd_returns_requirements(db_session, owned_jd):
    result = get_jd_capability(db_session, user_id="default", jd_id="jd-own")
    assert result["status"] == "succeeded"
    jd = result["jd"]
    assert jd["company"] == "某某科技"
    assert "Python" in jd["requirements"]["must_have"]
    assert "raw_text" not in jd
    # JD 无版本列，必须诚实标注而不是伪造
    assert "noversion" in result["object_version"]
    assert result["object_refs"][0]["version"] is None


def test_get_match_reads_existing(db_session, owned_jd, owned_resume):
    match = MatchResult(
        id="match-1",
        jd_id="jd-own",
        resume_id="resume-own",
        score=68.0,
        skill_match_detail={"matched": ["Python"], "missing": ["Redis"], "partial": ["Kubernetes"]},
        gap_analysis={"hard_gap": ["Redis"], "soft_gap": ["Kubernetes"], "suggestion": "补充 Redis 证据"},
    )
    db_session.add(match)
    db_session.commit()

    result = get_match_capability(
        db_session, user_id="default",
        resume_id="resume-own", jd_id="jd-own",
    )
    assert result["status"] == "succeeded"
    m = result["match"]
    assert m["score"] == 68.0
    assert m["highlights"] == ["Python"]
    assert m["hard_gaps"] == ["Redis"]
    assert m["soft_gaps"] == ["Kubernetes"]
    # 证据引用必须可用于追溯
    assert any("match-1" in ref for ref in result["evidence_refs"])
    # 双方对象引用都要有
    types = {r["type"] for r in result["object_refs"]}
    assert types == {"resume", "jd"}


def test_get_match_no_result_returns_not_found(db_session, owned_jd, owned_resume):
    """没有已有结果时不得触发重算，只返回 not_found。"""
    result = get_match_capability(
        db_session, user_id="default",
        resume_id="resume-own", jd_id="jd-own",
    )
    assert result["status"] == "not_found"
    assert result["reason"] == "no_match_result"


def test_get_match_tolerates_partial_legacy_data(db_session, owned_jd, owned_resume):
    """历史数据只填了 skill_match_detail 时，缺口仍要能看到。"""
    match = MatchResult(
        jd_id="jd-own",
        resume_id="resume-own",
        score=50.0,
        skill_match_detail={"matched": [], "missing": ["Go"], "partial": []},
        gap_analysis=None,  # 旧数据可能为空
    )
    db_session.add(match)
    db_session.commit()

    result = get_match_capability(
        db_session, user_id="default",
        resume_id="resume-own", jd_id="jd-own",
    )
    assert result["status"] == "succeeded"
    assert result["match"]["hard_gaps"] == ["Go"]
