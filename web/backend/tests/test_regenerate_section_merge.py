"""逐段重写：模型漏字段时不能整份失败（回归测试）。

背景（这是生产探针抓到的，离线单测此前抓不到）：
  逐段重写的提示词要求模型回吐**整份** structured JSON，只改指定字段。
  但真实模型经常只回被改的那一段 —— 生产实测一次 `resume_regenerate_section`
  的 completion 只有 559 tokens 就结束、`name` 缺失，于是 `_build_result`
  直接返回"生成结果缺少姓名，请重试"：用户点了"重写这一段"，**整份简历都拿不回来**。

  为什么单测一直绿：conftest 的假模型对这条提示词是 `payload = dict(current)`
  完美回吐，永远带齐所有字段。所以这里必须**显式模拟"只回一段"**的行为。

修复：`_merge_regenerate_payload` 只补空 ——
  * 模型没给 / 给了空值 → 回落到用户手上那份 structured；
  * 模型给了非空值 → 以模型为准；
  * education / experiences 条数少于来源 → 一律回落到来源（不许悄悄丢经历）。
"""
from app.modules.resume_generation.services import _merge_regenerate_payload

SOURCE = {
    "name": "张一",
    "position": "后端开发工程师",
    "contact": {"phone": "13800000000", "email": "a@example.com", "github": ""},
    "summary": "三年后端开发经验。",
    "education": [{"school": "某大学", "major": "计算机", "degree": "本科"}],
    "experiences": [
        {
            "type": "project",
            "name": "订单系统",
            "role": "后端开发",
            "start": "2024-06",
            "end": "2024-09",
            "bullets": ["接口 P99 从 800ms 降到 240ms"],
        },
        {
            "type": "internship",
            "name": "某公司实习",
            "role": "后端开发",
            "start": "2023-07",
            "end": "2023-09",
            "bullets": ["编写单元测试 120 条"],
        },
    ],
    "skills": ["Python", "FastAPI"],
    "certifications": [],
}


# ------------------------------------------------------------------ 纯函数层
def test_merge_fills_every_key_the_model_omitted():
    """生产现场：模型只回了 summary，其余字段必须从来源补齐。"""
    merged = _merge_regenerate_payload({"summary": "重写后的摘要。"}, SOURCE)
    assert merged["summary"] == "重写后的摘要。"      # 模型改的那段以模型为准
    assert merged["name"] == "张一"                   # 漏掉的事实字段从来源补
    assert merged["position"] == "后端开发工程师"
    assert merged["contact"] == SOURCE["contact"]
    assert merged["education"] == SOURCE["education"]
    assert [e["name"] for e in merged["experiences"]] == ["订单系统", "某公司实习"]
    assert merged["skills"] == ["Python", "FastAPI"]


def test_merge_never_lets_the_model_override_identity_facts():
    merged = _merge_regenerate_payload(
        {"name": "模型写的名字", "summary": "新摘要"}, SOURCE)
    assert merged["name"] == "张一"
    assert merged["summary"] == "新摘要"
    assert merged["skills"] == SOURCE["skills"]


def test_merge_treats_blank_values_as_missing():
    """模型给了空串 / 空数组，等于没给：不许把用户的内容清空。"""
    merged = _merge_regenerate_payload(
        {"name": "  ", "skills": [], "summary": None, "contact": {}}, SOURCE)
    assert merged["name"] == "张一"
    assert merged["skills"] == ["Python", "FastAPI"]
    assert merged["summary"] == "三年后端开发经验。"
    assert merged["contact"] == SOURCE["contact"]


def test_merge_refuses_to_drop_experiences():
    """模型只回一条经历 ≠ 可以删掉另一条：条数变少一律回落到来源。"""
    merged = _merge_regenerate_payload(
        {"experiences": [SOURCE["experiences"][0]]}, SOURCE)
    assert len(merged["experiences"]) == 2
    assert merged["experiences"] == SOURCE["experiences"]


def test_merge_drops_an_extra_experience_added_by_the_model():
    """模型多回一条就是在新增不存在的经历，必须丢弃而不是落库。"""
    extra = dict(SOURCE["experiences"][0], name="新增项目")
    merged = _merge_regenerate_payload(
        {"experiences": SOURCE["experiences"] + [extra]}, SOURCE)
    assert len(merged["experiences"]) == 2
    assert [item["name"] for item in merged["experiences"]] == ["订单系统", "某公司实习"]


def test_merge_keeps_source_identity_but_accepts_rewritten_bullets():
    changed = dict(
        SOURCE["experiences"][0],
        name="幻觉项目",
        role="架构师",
        start="2030-01",
        end="2030-12",
        bullets=["优化后的真实经历描述"],
    )
    merged = _merge_regenerate_payload({"experiences": [changed]}, SOURCE)
    first = merged["experiences"][0]
    assert first["name"] == "订单系统"
    assert first["role"] == "后端开发"
    assert first["start"] == "2024-06"
    assert first["end"] == "2024-09"
    assert first["bullets"] == ["优化后的真实经历描述"]


def test_merge_handles_non_dict_payload():
    """模型吐了非 JSON 对象：退回来源，而不是崩在半路。"""
    merged = _merge_regenerate_payload(None, SOURCE)
    assert merged["name"] == "张一"
    assert merged["experiences"] == SOURCE["experiences"]
    assert _merge_regenerate_payload("不是对象", SOURCE)["name"] == "张一"


# ------------------------------------------------------------------ 接口层
def _payload():
    return {
        "name": "张一",
        "position": "后端开发工程师",
        "phone": "13800000000",
        "email": "a@example.com",
        "summary": "三年后端开发经验，主导订单系统重构。",
        "direction": "技术研发",
        "education": [
            {"school": "某大学", "major": "计算机科学与技术",
             "degree": "本科", "start": "2021.09", "end": "2025.06"}
        ],
        "experiences": [
            {"company": "某科技", "role": "后端开发", "start": "2024.06", "end": "2024.09",
             "bullets": ["参与订单服务拆分，接口 P99 从 800ms 降到 240ms"]}
        ],
        "skills": ["Python", "FastAPI", "PostgreSQL"],
        "certifications": [],
        "jd_text": "负责后端服务设计与开发，要求熟悉 Python、FastAPI。",
    }


def _generate(client):
    resp = client.post("/api/v1/resume-generation/generate", json=_payload())
    assert resp.status_code == 200, resp.text
    return resp.json()["data"]


def _struct_of(d):
    return {
        "name": d["name"],
        "position": d["position"],
        "contact": d["contact"],
        "summary": d["summary"],
        "education": d["education"],
        "experiences": d["experiences"],
        "skills": d["skills"],
        "certifications": d["certifications"],
    }


def test_regenerate_section_survives_model_returning_only_the_edited_field(
        client, _fake_llm):
    """生产翻车的那个场景：模型只回 {"summary": ...}，接口必须照样成功。"""
    gen = _generate(client)
    _fake_llm.regenerate_handler = lambda current: {"summary": "重写后的个人总结。"}

    resp = client.post(
        "/api/v1/resume-generation/regenerate-section",
        json={
            "resume_id": gen["id"],
            "structured": _struct_of(gen),
            "section": "summary",
            "jd_text": "熟悉 Python 与 FastAPI",
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    # 修复前这里 error = "生成结果缺少姓名，请重试"，data = None
    assert body["error"] is None
    data = body["data"]
    assert data is not None

    # 被改的那段用模型产出
    assert "重写后的个人总结。" in data["summary"]
    # 其余事实字段一个都不能丢
    assert data["name"] == gen["name"]
    assert data["position"] == gen["position"]
    assert data["contact"] == gen["contact"]
    assert len(data["education"]) == len(gen["education"])
    assert len(data["experiences"]) == len(gen["experiences"])
    assert data["skills"] == gen["skills"]

    # 仍然是"追加新版本"，不是原地改写
    assert data["id"] != gen["id"]
    assert data["version"] == gen["version"] + 1
    versions = client.get(f"/api/v1/resume/{gen['id']}/versions").json()["data"]
    assert versions[0]["id"] == data["id"]


def test_regenerate_section_fails_closed_when_the_model_drops_a_whole_experience(
        client, _fake_llm):
    """模型连经历条数都少给了：不许把少掉的那段落库，回落来源后照常成功。"""
    gen = _generate(client)
    assert len(gen["experiences"]) >= 1
    _fake_llm.regenerate_handler = lambda current: {
        "experiences": [
            {
                "type": "project",
                "name": current["experiences"][0].get("name", ""),
                "role": current["experiences"][0].get("role", ""),
                "start": current["experiences"][0].get("start", ""),
                "end": current["experiences"][0].get("end", ""),
                "bullets": ["只回了一条经历"],
            }
        ]
    }

    resp = client.post(
        "/api/v1/resume-generation/regenerate-section",
        json={
            "resume_id": gen["id"],
            "structured": _struct_of(gen),
            "section": "experiences",
            "index": 0,
            "jd_text": "熟悉 Python 与 FastAPI",
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["error"] is None and body["data"] is not None
    assert len(body["data"]["experiences"]) == len(gen["experiences"])
