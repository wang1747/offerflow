"""一页约束：structured 与 resume_text 必须始终是**同一份内容**。

这组测试守的是一条真出过问题的线：超长简历被 LLM 精简的是**文本**，
但导出优先用 `parsed_json.structured` —— 两边不同步就会出现
「界面显示一页、导出却是两页」。

断言方式刻意写成「**独立重建 structured 再比对**」，
而不是断言某个预设字符串 —— 否则断言和实现犯了同一个错，绿也是假绿。
"""

import asyncio

from test_resume_generation import _generate_payload

from app.models.user import User
from app.modules.resume_generation import page_fit as P
from app.modules.resume_generation import services as S
from app.modules.resume_generation.render import render_resume_text

USER = User(id="default", name="默认用户")

PROJECT = "OfferFlow 求职系统"
INTERNSHIP = "某公司实习"


# ---------- 造数据 ----------


def _bullet(topic: str, index: int) -> str:
    return (
        f"负责{topic}模块第{index}个环节的后端接口开发与联调，"
        "梳理调用链路并补齐异常处理，保证服务稳定运行"
    )


def _payload(per_experience: int = 15) -> dict:
    """参数是单段经历的要点条数；默认共 30 条，渲染后必定超过一页。"""
    return {
        "name": "张三",
        "position": "Python 后端实习",
        "contact": {"phone": "13800138000", "email": "zhangsan@example.com", "github": ""},
        "summary": "计算机科学与技术专业，具备 Python 后端开发能力，熟悉常见工程实践。",
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
                "name": PROJECT,
                "role": "后端开发",
                "start": "2026-03",
                "end": "2026-08",
                "bullets": [_bullet("订单结算", i) for i in range(per_experience)],
            },
            {
                "type": "internship",
                "name": INTERNSHIP,
                "role": "开发实习生",
                "start": "2026-06",
                "end": "2026-08",
                "bullets": [_bullet("数据同步", i) for i in range(per_experience)],
            },
        ],
        "skills": ["Python", "FastAPI", "Docker"],
        "certifications": [],
    }


def _structured_of(result) -> dict:
    """从 GenerationResult 独立重建 structured（不复用生产代码的那份函数）。"""
    return {
        "name": result.name,
        "position": result.position,
        "contact": result.contact,
        "summary": result.summary,
        "education": result.education,
        "experiences": result.experiences,
        "skills": result.skills,
        "certifications": result.certifications,
    }


def _condensed(payload: dict, project_bullets: int = 1, keep_internship: bool = False) -> str:
    """按 render_resume_text 的格式造一份"三刀裁剪后"的文本。"""
    exp = payload["experiences"][0]
    lines = [
        payload["name"],
        "13800138000 | zhangsan@example.com",
        "",
        "求职意向",
        payload["position"],
        "",
        "个人总结",
        "计算机科学与技术专业，具备后端开发能力。",
        "",
        "教育经历",
        "示例大学 计算机科学与技术 本科 2023-09 ~ 2027-06",
        "",
        "项目经历",
        f"{exp['name']}（{exp['role']}，{exp['start']} ~ {exp['end']}）",
    ]
    lines += ["- " + b for b in exp["bullets"][:project_bullets]]
    if keep_internship:
        other = payload["experiences"][1]
        lines += [
            "",
            "实习经历",
            f"{other['name']}（{other['role']}，{other['start']} ~ {other['end']}）",
        ]
        lines += ["- " + b for b in other["bullets"]]
    lines += ["", "技能", "Python、FastAPI"]
    return "\n".join(lines)


def _keep_bullet_ratio(ratio: float):
    """构造"三刀裁剪"结果：按比例保留要点行，其余删除。"""

    def _handler(text: str) -> tuple:
        lines = text.splitlines()
        indexes = [i for i, line in enumerate(lines) if line.strip().startswith("- ")]
        keep = set(indexes[: max(1, int(len(indexes) * ratio))])
        drop = set(indexes) - keep
        kept_text = "\n".join(line for i, line in enumerate(lines) if i not in drop)
        return kept_text, [f"删除了 {len(drop)} 条要点"]

    return _handler


# ---------- 纯函数：从精简文本重建 structured ----------


def test_restructure_adopts_condensed_wording_and_drops_deleted_bullets():
    base = _payload(3)
    text = _condensed(base, project_bullets=1)

    rebuilt = P.restructure_from_text(base, text)

    # 取值必须来自精简文本（这就是"从精简文本重新提取"）
    assert rebuilt["summary"] == "计算机科学与技术专业，具备后端开发能力。"
    project = rebuilt["experiences"][0]
    assert project["bullets"] == [base["experiences"][0]["bullets"][0]]
    # 事实层一个都没动
    assert project["name"] == PROJECT
    assert project["role"] == "后端开发"
    assert project["start"] == "2026-03"
    assert rebuilt["education"] == base["education"]
    # 技能做交集：Docker 在精简文本里没了 → 不再保留
    assert rebuilt["skills"] == ["Python", "FastAPI"]


def test_restructure_removes_whole_experience_only_when_it_disappeared():
    base = _payload(3)
    text = _condensed(base, project_bullets=1, keep_internship=False)

    rebuilt = P.restructure_from_text(base, text)

    assert [e["name"] for e in rebuilt["experiences"]] == [PROJECT]


def test_restructure_keeps_experience_when_it_is_still_present():
    base = _payload(3)
    text = _condensed(base, project_bullets=1, keep_internship=True)

    rebuilt = P.restructure_from_text(base, text)

    assert [e["name"] for e in rebuilt["experiences"]] == [PROJECT, INTERNSHIP]
    assert len(rebuilt["experiences"][1]["bullets"]) == 3


def test_bullets_are_matched_within_their_own_experience_block():
    """要点只在**自己那段经历**的块里匹配，绝不窜到另一段经历去。

    简历里要点长得都很像（"负责 A 模块…"vs"负责 B 模块…"），
    全池匹配会让 A 段把 B 段的要点认领过来 —— 那是比"删多了"更糟的数据污染。
    """
    base = _payload(2)
    project_bullet = base["experiences"][0]["bullets"][0]
    text = "\n".join([
        base["name"],
        "",
        "项目经历",
        f"{PROJECT}（后端开发，2026-03 ~ 2026-08）",
        "- " + project_bullet,
        "",
        "实习经历",
        f"{INTERNSHIP}（开发实习生，2026-06 ~ 2026-08）",  # 块里一条要点都没有
    ])

    rebuilt = P.restructure_from_text(base, text)

    project, internship = rebuilt["experiences"]
    assert project["bullets"] == [project_bullet]
    # 实习这块没有要点 → 就是空的，不许拿项目那段（长得很像）的要点顶上
    assert internship["bullets"] == []
    assert "订单结算" not in str(internship)


def test_restructure_falls_back_when_condensed_text_has_no_bullets():
    """精简输出不可用（要点全丢）→ 整体回退，绝不静默产出残缺简历。"""
    base = _payload(3)

    assert P.restructure_from_text(base, "张三\n一段没有任何要点的文字") is base
    assert P.restructure_from_text(base, "   ") is base


# ---------- 生成链路：精简后 structured 与 resume_text 一致 ----------


def test_build_result_fails_closed_when_model_drops_an_input_experience():
    payload = _payload(2)
    payload["experiences"] = payload["experiences"][:1]
    exp_input = [
        {
            "type": item["type"],
            "name": item["name"],
            "role": item["role"],
            "start": item["start"],
            "end": item["end"],
        }
        for item in _payload(2)["experiences"]
    ]

    result = asyncio.run(S._build_result(payload, "", None, "", USER, None, exp_input))

    assert result.error
    assert "经验" in result.error or "经历" in result.error


def test_build_result_keeps_structured_and_text_in_sync_after_condense(_fake_llm):
    payload = _payload(15)
    original_text = render_resume_text(payload)
    assert P.is_too_long(original_text), "测试数据本身必须超过一页，否则这条测试没意义"

    _fake_llm.condense_handler = _keep_bullet_ratio(0.3)
    result = asyncio.run(S._build_result(payload, "", None, "", USER))

    assert not result.error
    # 核心断言：最终文本必须**恰好**由最终 structured 渲染而来
    assert result.resume_text == render_resume_text(_structured_of(result))
    assert len(result.resume_text) < len(original_text)
    assert any("已自动精简到一页" in t for t in result.tips)
    # 被删掉的要点不能还留在 structured 里（否则导出又会变回两页）
    kept = [b for e in result.experiences for b in e["bullets"]]
    assert 0 < len(kept) < 30
    assert all(b in result.resume_text for b in kept)


def test_build_result_does_not_claim_condense_when_llm_changed_nothing(_fake_llm):
    payload = _payload(15)
    _fake_llm.condense_handler = lambda text: (text, [])  # LLM 原样返回

    result = asyncio.run(S._build_result(payload, "", None, "", USER))

    assert not result.error
    assert not any("已自动精简到一页" in t for t in result.tips)  # 没精简成功就不许谎报
    assert result.resume_text == render_resume_text(_structured_of(result))


# ---------- 二次精简 ----------


def test_enforce_one_page_second_condense_succeeds(_fake_llm):
    payload = _payload(15)
    source_text = render_resume_text(payload)

    calls = {"n": 0}

    def _handler(text: str) -> tuple:
        calls["n"] += 1
        # 第一次只删掉一点点 → 仍超一页；第二次删得更狠 → 落到一页内
        return _keep_bullet_ratio(0.9 if calls["n"] == 1 else 0.2)(text)

    _fake_llm.condense_handler = _handler
    result = asyncio.run(S._build_result(payload, "", None, "", USER))
    assert P.is_too_long(result.resume_text), "第一次精简后应当仍然超长，才谈得上二次精简"

    result = asyncio.run(S._enforce_one_page(result, source_text, USER, ""))

    assert not result.error
    assert not P.is_too_long(result.resume_text)
    assert any("已二次精简" in t for t in result.tips)
    assert result.resume_text == render_resume_text(_structured_of(result))  # 仍必须一致
    # 精简后重算了报告，不是沿用精简前的
    assert result.sections
    assert result.quality


def test_enforce_one_page_fails_closed_when_still_too_long(_fake_llm):
    payload = _payload(15)
    source_text = render_resume_text(payload)
    _fake_llm.condense_handler = lambda text: (text, [])  # 两次都精简不动

    result = asyncio.run(S._build_result(payload, "", None, "", USER))
    result = asyncio.run(S._enforce_one_page(result, source_text, USER, ""))

    assert result.error
    assert "超过一页" in result.error
    assert P.is_too_long(result.resume_text)


# ---------- 端到端：一页约束在"保存"这一步拦住落库 ----------


def _generate_and_get_id(client) -> str:
    generated = client.post("/api/v1/resume-generation/generate", json=_generate_payload())
    assert generated.json()["success"] is True
    return generated.json()["data"]["id"]


def test_save_fails_closed_and_persists_nothing_when_still_too_long(client, _fake_llm):
    _fake_llm.condense_handler = lambda text: (text, [])  # 精简无效
    resume_id = _generate_and_get_id(client)
    before = client.get(f"/api/v1/resume/{resume_id}/versions").json()["data"]

    response = client.post(
        "/api/v1/resume-generation/save",
        json={"resume_id": resume_id, "structured": _payload(15)},
    )

    body = response.json()
    assert body["success"] is False
    assert "超过一页" in body["error"]
    # fail closed：一条记录都不许新增
    after = client.get(f"/api/v1/resume/{resume_id}/versions").json()["data"]
    assert [v["id"] for v in after] == [v["id"] for v in before]


def test_save_succeeds_and_persists_consistent_structured_when_condense_works(client, _fake_llm):
    _fake_llm.condense_handler = _keep_bullet_ratio(0.2)
    resume_id = _generate_and_get_id(client)

    response = client.post(
        "/api/v1/resume-generation/save",
        json={"resume_id": resume_id, "structured": _payload(15)},
    )
    body = response.json()
    assert body["success"] is True

    data = body["data"]
    assert not P.is_too_long(data["resume_text"])
    # 落库的 raw_text 必须与落库的 structured 严格自洽 —— 这正是"导出与界面一致"的前提
    assert data["resume_text"] == render_resume_text({
        "name": data["name"],
        "position": data["position"],
        "contact": data["contact"],
        "summary": data["summary"],
        "education": data["education"],
        "experiences": data["experiences"],
        "skills": data["skills"],
        "certifications": data["certifications"],
    })

    # 库里存的也是同一份内容（导出直接读它，所以这一条才是"一页不会变两页"的保证）
    versions = client.get(f"/api/v1/resume/{resume_id}/versions").json()["data"]
    latest = versions[0]
    assert latest["version"] == data["version"]
    assert latest["raw_text"].strip() == data["resume_text"].strip()
    assert latest["parsed_json"]["structured"]["summary"] == data["summary"]
