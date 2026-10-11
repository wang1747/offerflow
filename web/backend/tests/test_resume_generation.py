from app.modules.resume.structure import parse_resume_structure
from app.modules.resume_generation.render import render_resume_text


def test_render_resume_text_is_parseable():
    structured = {
        "name": "张三",
        "position": "Python 后端实习",
        "contact": {
            "phone": "13800138000",
            "email": "zhangsan@example.com",
            "github": "https://github.com/zhangsan",
        },
        "summary": "计算机科学与技术专业，具备 Python 后端开发能力。",
        "education": [
            {
                "school": "示例大学",
                "major": "计算机科学与技术",
                "degree": "本科",
                "start": "2023-09",
                "end": "2027-06",
                "detail": "",
            }
        ],
        "experiences": [
            {
                "type": "project",
                "name": "OfferFlow 求职系统",
                "role": "后端开发",
                "start": "2026-03",
                "end": "2026-08",
                "bullets": ["使用 FastAPI 搭建后端"],
            }
        ],
        "skills": ["Python", "FastAPI"],
        "certifications": [],
    }
    text = render_resume_text(structured)
    parsed = parse_resume_structure(text)
    assert parsed.name == "张三"
    titles = [section.title for section in parsed.sections]
    assert "教育经历" in titles
    assert "项目经历" in titles


def _generate_payload() -> dict:
    """简历生成请求体（与 app/modules/resume_generation/schemas.py 的
    ResumeGenerateRequest 保持一致：education / experiences / skills 均为结构化列表，
    不再是早期的字符串字段）。"""
    return {
        "name": "张三",
        "position": "Python 后端实习",
        "phone": "13800138000",
        "email": "zhangsan@example.com",
        "education": [
            {
                "school": "示例大学",
                "major": "计算机科学与技术",
                "degree": "本科",
                "start": "2023-09",
                "end": "2027-06",
            }
        ],
        "experiences": [
            {
                "type": "project",
                "name": "OfferFlow 求职 Agent 系统",
                "role": "后端开发",
                "start": "2026-03",
                "end": "2026-08",
                "bullets": ["使用 FastAPI 搭建后端服务"],
            }
        ],
        "skills": ["Python", "FastAPI", "Docker"],
        "jd_text": "Python 后端实习，要求熟悉 FastAPI",
    }


def test_resume_generate_api(client):
    response = client.post(
        "/api/v1/resume-generation/generate",
        json=_generate_payload(),
    )
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["resume_text"]
    assert data["id"]
    assert data["version"] == 1
    assert data["name"] == "张三"
    assert data["experiences"]
    assert "教育经历" in data["sections"]

    exported = client.get(f"/api/v1/resume/{data['id']}/export?format=word")
    assert exported.status_code == 200


def test_resume_regenerate_section_api(client):
    generate = client.post(
        "/api/v1/resume-generation/generate",
        json=_generate_payload(),
    )
    gen_data = generate.json()["data"]

    response = client.post(
        "/api/v1/resume-generation/regenerate-section",
        json={
            "resume_id": gen_data["id"],
            "structured": {
                "name": gen_data["name"],
                "position": gen_data["position"],
                "contact": gen_data["contact"],
                "summary": gen_data["summary"],
                "education": gen_data["education"],
                "experiences": gen_data["experiences"],
                "skills": gen_data["skills"],
                "certifications": gen_data["certifications"],
            },
            "section": "summary",
            "jd_text": "Python 后端实习，要求熟悉 FastAPI",
        },
    )
    assert response.status_code == 200
    data = response.json()["data"]
    # 逐段重写 = **追加新版本**（旧版本不可变）：id 变、version 递增、同一份 document
    assert data["id"] != gen_data["id"]
    assert data["version"] == gen_data["version"] + 1
    assert data["resume_text"]
    assert "已重新生成" in data["tips"]
    assert "score" in data["ats"]

    # 旧版本原样保留，新版本挂在同一份简历下（回滚才有地方可回）
    versions = client.get(f"/api/v1/resume/{gen_data['id']}/versions").json()["data"]
    assert [v["version"] for v in versions] == [2, 1]
    assert versions[0]["id"] == data["id"]
    assert versions[0]["parsed_json"]["document_id"] == versions[1]["parsed_json"]["document_id"]
