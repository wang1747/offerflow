"""bullet 级事实保真测试（10 步计划第 ④ 步）。

覆盖三层：
1. 数字归一化：中文数字/百分之/万级数量/日期必须能"同义归一"，否则比对全是假阳性。
2. 检出：编造数字、编造年份是硬违规；角色越权、编造技术栈是风险提示。
3. 确定性修复：编造数字必须被摘掉，而**用户确实提供过的数字一个都不能动**（对照）。
4. 端到端：离线 LLM 故意返回用户没提供过的「日均 1000 次调用」，断言最终产物里它被清除，
   且保真报告里留下了"检出过"的证据。
"""

from app.modules.resume_generation.fidelity import (
    canonical_numbers,
    check_bullet_fidelity,
    sanitize_structured,
    sanitize_text,
)

# 与 conftest.FakeLLM 的「资深简历撰写专家」分支保持一致的原始输入
SOURCE_TEXT = "\n".join([
    "张三",
    "Python 后端实习",
    "13800138000",
    "zhangsan@example.com",
    "使用 FastAPI 搭建后端服务",
    "OfferFlow 求职 Agent 系统",
    "后端开发",
    "2026-03",
    "2026-08",
    "Python",
    "FastAPI",
    "Docker",
    "示例大学",
    "2023-09",
    "2027-06",
])
ALLOWED = canonical_numbers(SOURCE_TEXT)


# ===== 1. 归一化 =====


def test_chinese_numerals_normalize_to_arabic():
    """「三个」和「3 个」必须归一到同一个 token，否则中文简历全是假阳性。"""
    assert canonical_numbers("打通了三个模块") == {"3"}
    assert canonical_numbers("打通了 3 个模块") == {"3"}
    assert canonical_numbers("累计十三次迭代") == {"13"}
    assert canonical_numbers("服务一百二十万用户") == {"1200000"}


def test_big_multiplier_and_percent_normalize():
    assert canonical_numbers("支撑 10万次调用") == {"100000"}
    assert canonical_numbers("支撑 100000 次调用") == {"100000"}
    assert canonical_numbers("提升百分之四十") == {"40%"}
    assert canonical_numbers("提升 40%") == {"40%"}


def test_units_with_meaning_are_kept():
    """有语义的单位不能被剥掉：「3 个月」「800ms」「3 年」都不是纯量词。"""
    assert canonical_numbers("历时 3 个月") == {"3个月"}
    assert canonical_numbers("响应 800ms") == {"800ms"}
    assert canonical_numbers("3 年经验") == {"3年"}


def test_quantity_without_measure_word_normalizes():
    """「十万用户」没有量词，必须和带量词/阿拉伯数字的写法全部等价。"""
    assert (
        canonical_numbers("服务十万用户")
        == canonical_numbers("服务 10万用户")
        == canonical_numbers("服务 100000 用户")
        == canonical_numbers("服务 100000 个用户")
        == {"100000"}
    )


def test_measure_word_does_not_split_the_number():
    """回归（生产实测捕获的真 bug）：量词若参与 token，「十万用户」归一到 `100000`、
    「100000 个用户」归一到 `100000个` → 同一事实被判成"编造"并**删掉合法数字**。
    修复后：任意写法互为等价，一个数字都不许删。
    """
    variants = ["服务十万用户", "服务 10万用户", "服务 100000 用户", "服务 100000 个用户"]
    # 正反两个方向都要成立：源里怎么写，生成侧换写法都不该被判编造
    for source in variants:
        allowed = canonical_numbers(source)
        for written in variants:
            text, removed = sanitize_text(written, allowed)
            assert removed == [], f"源={source!r} 生成={written!r} 被误删={removed}"
            assert text == written, f"源={source!r} 生成={written!r} 被改写={text!r}"

    # 量词互换（家/位）同样不该被判成另一个数字
    assert sanitize_text("服务 50 位客户", canonical_numbers("服务 50 家客户"))[1] == []
    # 但数值本身仍然必须可溯源：源里没给过的数字照样要被摘掉
    assert sanitize_text("服务 500 位客户", canonical_numbers("服务 50 家客户"))[1] != []


def test_common_measure_words_are_recognized():
    """回归（修前实测漏检）：`位`/`名`/`份`/`台` 等常用量词不在单位表里时，
    「服务 50 位客户」这类数字**完全不被识别** —— 既漏报也漏检。
    """
    cases = {
        "服务 50 位客户": "50",
        "带领 10 名成员": "10",
        "交付 3 份报告": "3",
        "部署 12 台机器": "12",
        "覆盖 8 个省份": "8",
    }
    for text, expect in cases.items():
        assert expect in canonical_numbers(text), f"{text!r} 应识别出 {expect}"
    # 识别得出来，才能拦得住
    assert sanitize_text("服务 500 位客户", canonical_numbers("服务 50 位客户"))[1] != []


def test_date_and_non_numbers_are_not_misread():
    """日期要能拆出年份；普通词语里的中文数字不能被当成量化成果。"""
    assert {"2023年", "09月"} <= canonical_numbers("2023-09")
    for harmless in ("保持一致", "第三方依赖", "十分满意", "数万次请求", "统一接口", "统一系统"):
        assert canonical_numbers(harmless) == set(), harmless


# ===== 2. 检出 =====


def _structured(bullets, summary="", role="后端开发"):
    return {
        "summary": summary,
        "experiences": [{
            "type": "project",
            "name": "OfferFlow 求职 Agent 系统",
            "role": role,
            "start": "2026-03",
            "end": "2026-08",
            "bullets": bullets,
        }],
    }


def test_fabricated_number_is_hard_violation():
    report = check_bullet_fidelity(
        _structured(["使用 FastAPI 搭建后端，支撑日均 1000 次调用"]), SOURCE_TEXT, ["后端开发"]
    )
    assert not report.passed
    assert report.hard_count == 1
    assert report.fabricated_numbers == ["1000 次"]
    assert report.issues[0]["section"] == "experiences[0].bullets[0]"
    assert report.issues[0]["kind"] == "number"


def test_user_provided_number_is_not_flagged():
    """对照：数字确实来自用户输入时必须放过（否则会把真话也删掉）。"""
    report = check_bullet_fidelity(
        _structured(["覆盖 3 个模块的开发，使用 FastAPI 搭建后端"]), SOURCE_TEXT + "\n3 个", ["后端开发"]
    )
    assert report.passed
    assert report.fabricated_numbers == []


def test_fabricated_year_is_hard_violation():
    """年份对不上（用户只填了 2023/2026/2027）必须是硬违规。"""
    report = check_bullet_fidelity(
        _structured(["2024 年完成系统上线"]), SOURCE_TEXT, ["后端开发"]
    )
    assert not report.passed
    assert report.hard_count >= 1
    assert "2024 年" in report.fabricated_numbers


def test_bare_fabricated_year_without_suffix_is_hard_violation():
    report = check_bullet_fidelity(
        _structured(["于 2024 完成系统上线"]), SOURCE_TEXT, ["后端开发"]
    )
    assert not report.passed
    assert any(i["kind"] == "year" for i in report.issues)


def test_year_from_experience_dates_is_accepted():
    report = check_bullet_fidelity(
        _structured(["2026 年完成系统上线"]), SOURCE_TEXT, ["后端开发"]
    )
    assert report.passed


def test_role_overclaim_is_warning_only():
    report = check_bullet_fidelity(
        _structured(["作为技术负责人牵头整条链路，使用 FastAPI 搭建后端"]), SOURCE_TEXT, ["后端开发"]
    )
    assert report.passed  # 只警告，不算硬违规
    roles = [i for i in report.issues if i["kind"] == "role"]
    # 同一条 bullet 只报最长的一个身份词，不重复刷屏
    assert len(roles) == 1
    assert "技术负责人" in roles[0]["detail"]


def test_role_overclaim_allowed_when_user_role_matches():
    report = check_bullet_fidelity(
        _structured(["作为技术负责人牵头整条链路，使用 FastAPI 搭建后端"], role="技术负责人"),
        SOURCE_TEXT,
        ["技术负责人"],
    )
    assert [i for i in report.issues if i["kind"] == "role"] == []


def test_unknown_tech_name_is_warning_only():
    report = check_bullet_fidelity(
        _structured(["引入 Kubernetes 完成向量检索改造"]), SOURCE_TEXT, ["后端开发"]
    )
    assert report.passed
    assert any(
        i["kind"] == "tech" and "Kubernetes" in i["detail"] for i in report.issues
    )


def test_summary_numbers_are_checked_too():
    report = check_bullet_fidelity(
        _structured([], summary="主导 3 个项目落地"), SOURCE_TEXT, ["后端开发"]
    )
    assert not report.passed
    assert report.issues[0]["section"] == "summary"


def test_feedback_hint_carries_section_level_locations():
    report = check_bullet_fidelity(
        _structured(["使用 FastAPI 搭建后端，支撑日均 1000 次调用"]), SOURCE_TEXT, ["后端开发"]
    )
    assert "experiences[0].bullets[0]" in report.feedback_hint
    assert "1000 次" in report.feedback_hint


# ===== 3. 确定性修复 =====


def test_sanitize_drops_whole_result_clause():
    """结果从句整体是编造的 → 整条从句删掉，句子仍然通顺。"""
    text, removed = sanitize_text("使用 FastAPI 搭建后端，支撑日均 1000 次调用", ALLOWED)
    assert text == "使用 FastAPI 搭建后端"
    assert removed == ["1000 次"]


def test_sanitize_keeps_the_rest_when_only_the_number_is_fake():
    text, removed = sanitize_text("主导 3 个模块开发，提升接口性能 40%", ALLOWED)
    assert text == "主导模块开发，提升接口性能"
    assert set(removed) == {"3 个", "40%"}


def test_sanitize_drops_unsalvageable_bullet():
    """「从 X 到 Y」两头都是编的 → 句子救不回来，宁可整条丢掉也不留残句。"""
    text, removed = sanitize_text("将部署耗时从 30 分钟缩短到 5 分钟", ALLOWED)
    assert text == ""
    assert set(removed) == {"30 分钟", "5 分钟"}


def test_sanitize_is_a_noop_when_everything_is_traceable():
    """对照：全部可溯源时必须**逐字不动**（连日期写法都不能被改写）。"""
    original = "使用 FastAPI 搭建后端服务，2026-03 上线"
    allowed = canonical_numbers(SOURCE_TEXT)
    assert sanitize_text(original, allowed) == (original, [])


def test_sanitize_is_idempotent():
    once, _ = sanitize_text("优化 5 个核心接口，支撑日均 3000 次调用", ALLOWED)
    twice, removed = sanitize_text(once, ALLOWED)
    assert twice == once
    assert removed == []


def test_sanitize_structured_edits_in_place_and_drops_empty_bullets():
    data = _structured([
        "使用 FastAPI 搭建后端，支撑日均 1000 次调用",
        "服务 50 家客户",
        "使用 FastAPI 搭建后端服务",
    ])
    new, removed = sanitize_structured(data, SOURCE_TEXT)
    assert new is data  # 就地修改，调用方拿到的就是改过的那份
    bullets = new["experiences"][0]["bullets"]
    assert bullets == ["使用 FastAPI 搭建后端", "使用 FastAPI 搭建后端服务"]
    assert set(removed) == {"1000 次", "50 家"}
    assert all("1000" not in b and "50" not in b for b in bullets)


def test_after_sanitize_no_hard_violation_remains():
    data = _structured([
        "使用 FastAPI 搭建后端，支撑日均 1000 次调用",
        "主导模块化拆分，把部署耗时从 30 分钟缩短到 5 分钟",
    ])
    sanitize_structured(data, SOURCE_TEXT)
    assert check_bullet_fidelity(data, SOURCE_TEXT, ["后端开发"]).hard_count == 0


# ===== 4. 端到端 =====


def test_generate_api_strips_unverifiable_numbers(client):
    """离线 LLM 会返回用户没提供过的「支撑日均 1000 次调用」，必须被摘掉。"""
    from test_resume_generation import _generate_payload

    response = client.post("/api/v1/resume-generation/generate", json=_generate_payload())
    assert response.status_code == 200
    data = response.json()["data"]

    bullets = [
        b
        for exp in data["experiences"]
        for b in (exp.get("bullets") or [])
    ]
    # 正反两面都断言：既证明"检出来了"，也证明"真的删掉了"
    assert "1000 次" in data["fidelity"]["bullets"]["fabricated_numbers"]
    assert not data["fidelity"]["bullets"]["passed"]
    assert all("1000" not in b for b in bullets), bullets
    assert "1000" not in data["resume_text"]
    assert any("事实保真" in t for t in data["tips"])
    assert any(r.get("level") == "warn" and "数字" in r.get("issue", "") for r in data["risks"])


def test_generate_api_keeps_legit_content_after_sanitize(client):
    """净化不能把整份简历删空——删完仍要能渲染、仍要有教育经历与项目要点。"""
    from test_resume_generation import _generate_payload

    response = client.post("/api/v1/resume-generation/generate", json=_generate_payload())
    data = response.json()["data"]
    assert data["resume_text"].strip()
    assert "教育经历" in data["sections"]
    assert data["experiences"][0]["bullets"]


def test_regenerate_section_reports_bullet_fidelity(client):
    """逐段重写路径只报不改：报告里必须带上 bullets 维度。"""
    from test_resume_generation import _generate_payload

    gen = client.post("/api/v1/resume-generation/generate", json=_generate_payload()).json()["data"]
    response = client.post(
        "/api/v1/resume-generation/regenerate-section",
        json={
            "resume_id": gen["id"],
            "structured": {
                "name": gen["name"],
                "position": gen["position"],
                "contact": gen["contact"],
                "summary": gen["summary"],
                "education": gen["education"],
                "experiences": gen["experiences"],
                "skills": gen["skills"],
                "certifications": gen["certifications"],
            },
            "section": "summary",
            "jd_text": "Python 后端实习，要求熟悉 FastAPI",
        },
    )
    assert response.status_code == 200
    body = response.json()["data"]
    assert "bullets" in body["fidelity"]
    assert body["fidelity"]["bullets"]["passed"] is True
