import pytest
from app.models.jd import JobDescription


def test_jd_model_creation(db_session):
    """测试 JD 模型创建"""
    jd = JobDescription(
        user_id="test_user",
        raw_text="Test JD text",
        company="TestCorp",
        position="Engineer",
    )
    db_session.add(jd)
    db_session.commit()
    assert jd.id is not None
    assert jd.company == "TestCorp"
    assert jd.position == "Engineer"


def test_jd_parse_api(client, sample_jd_text):
    """测试 JD 解析 API"""
    response = client.post("/api/v1/jd/parse", json={"raw_text": sample_jd_text})
    assert response.status_code == 200
    data = response.json()
    assert data["success"] is True
    parsed = data["data"]["parsed"]
    assert parsed["company"] == "ByteDance"
    assert "Python" in parsed["must_have"]


def test_jd_list_empty(db_session):
    """测试空 JD 列表"""
    from app.config import get_settings
    settings = get_settings()
    jds = db_session.query(JobDescription).filter(
        JobDescription.user_id == settings.default_user_id
    ).all()
    assert len(jds) == 0


def test_jd_update_api(client, sample_jd_text):
    """测试手动修改 JD 公司/职位"""
    response = client.post("/api/v1/jd/parse", json={"raw_text": sample_jd_text})
    jd_id = response.json()["data"]["id"]

    update = client.put(
        f"/api/v1/jd/{jd_id}",
        json={"company": "腾讯", "position": "高级后端工程师"},
    )
    assert update.status_code == 200
    data = update.json()["data"]
    assert data["company"] == "腾讯"
    assert data["position"] == "高级后端工程师"

    listing = client.get("/api/v1/jd/list").json()["data"]
    assert listing[0]["company"] == "腾讯"
    assert listing[0]["position"] == "高级后端工程师"


def test_jd_update_requires_field(client, sample_jd_text):
    """测试公司/职位都为空时返回 400"""
    response = client.post("/api/v1/jd/parse", json={"raw_text": sample_jd_text})
    jd_id = response.json()["data"]["id"]

    update = client.put(f"/api/v1/jd/{jd_id}", json={})
    assert update.status_code == 400


def test_jd_update_not_found(client):
    """测试更新不存在的 JD 返回 404"""
    update = client.put("/api/v1/jd/nonexistent", json={"company": "腾讯"})
    assert update.status_code == 404
