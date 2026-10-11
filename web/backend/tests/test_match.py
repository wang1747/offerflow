import pytest
from app.models.match import MatchResult


def test_match_result_creation(db_session):
    """测试匹配结果模型创建"""
    match = MatchResult(
        jd_id="jd-1",
        resume_id="resume-1",
        score=85.5,
    )
    db_session.add(match)
    db_session.commit()
    assert match.id is not None
    assert match.score == 85.5
    assert match.jd_id == "jd-1"
    assert match.resume_id == "resume-1"


def test_match_default_score(db_session):
    """测试匹配分默认值"""
    match = MatchResult(jd_id="jd-2", resume_id="resume-2", score=0.0)
    db_session.add(match)
    db_session.commit()
    assert match.score == 0.0
    assert match.skill_match_detail is None
    assert match.gap_analysis is None
    assert match.suggestion is None
    assert match.created_at is not None
    assert match.jd is None
    assert match.resume is None


def test_match_api_persists_and_ranks(client):
    """测试匹配接口落库、详情和排名"""
    jd_resp = client.post(
        "/api/v1/jd/parse",
        json={"raw_text": "Python backend, Docker, Redis, 高并发, FastAPI"},
    )
    assert jd_resp.json()["success"] is True
    jd_id = jd_resp.json()["data"]["id"]

    resume_resp = client.post(
        "/api/v1/resume/upload",
        json={"raw_text": "Python developer, familiar with FastAPI, Redis and Docker", "source_file": "test.txt"},
    )
    assert resume_resp.json()["success"] is True
    resume_id = resume_resp.json()["data"]["id"]

    response = client.post("/api/v1/match", json={"jd_id": jd_id, "resume_id": resume_id})
    assert response.status_code == 200
    body = response.json()
    assert body["success"] is True
    assert body["data"]["score"] >= 0
    assert "missing" in body["data"]["skill_match_detail"]
    assert "Python" in body["data"]["skill_match_detail"]["matched"]

    match_id = body["data"]["id"]
    detail = client.get(f"/api/v1/match/{match_id}")
    assert detail.status_code == 200
    assert detail.json()["data"]["id"] == match_id

    rankings = client.get("/api/v1/match/rankings")
    assert rankings.status_code == 200
    assert len(rankings.json()["data"]) == 1
    assert rankings.json()["data"][0]["jd_id"] == jd_id
