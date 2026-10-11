from app.models.interview import InterviewArticle, InterviewQuestion


def test_article_creation(db_session):
    """测试面经文章创建"""
    article = InterviewArticle(
        user_id="test_user",
        company="ByteDance",
        position="Backend",
        raw_content="Interview experience content",
        source="manual",
    )
    db_session.add(article)
    db_session.commit()
    assert article.id is not None
    assert article.company == "ByteDance"


def test_question_creation(db_session):
    """测试面试题创建"""
    question = InterviewQuestion(
        user_id="test_user",
        question="What is Python?",
        answer="A programming language",
        category="技术",
    )
    db_session.add(question)
    db_session.commit()
    assert question.id is not None
    assert question.category == "技术"


def test_article_question_relation(db_session):
    """测试面经与题目的关联"""
    article = InterviewArticle(
        user_id="test_user",
        company="TestCorp",
        position="Backend",
        raw_content="Some content",
        source="manual",
    )
    db_session.add(article)
    db_session.flush()

    question = InterviewQuestion(
        user_id="test_user",
        article_id=article.id,
        question="Test question?",
    )
    db_session.add(question)
    db_session.commit()

    assert question.article_id == article.id
    assert article.question_list[0].id == question.id
    assert question.article.company == "TestCorp"
    assert question.article.raw_content == "Some content"
    assert question.article.position == "Backend"
    assert question.article.source == "manual"
    assert article.created_at is not None
    assert article.embedding_id is None
    assert article.tags is None
    assert article.difficulty is None


def test_article_import_dedup(client):
    """测试面经导入去重"""
    content = "字节跳动后端一面：自我介绍，讲项目，Redis 缓存穿透。"
    first = client.post(
        "/api/v1/interview/articles",
        json={"company": "ByteDance", "raw_content": content},
    )
    assert first.json()["success"] is True
    first_id = first.json()["data"]["id"]

    second = client.post(
        "/api/v1/interview/articles",
        json={"company": "ByteDance", "raw_content": content + "  "},
    )
    assert second.json()["data"]["duplicate"] is True
    assert second.json()["data"]["id"] == first_id


def test_article_file_upload(client):
    """测试面经文件上传"""
    response = client.post(
        "/api/v1/interview/articles/upload",
        data={"company": "TestCorp"},
        files={"file": ("article.md", "# 面经\n问题：什么是高并发？".encode("utf-8"), "text/markdown")},
    )
    assert response.status_code == 200
    assert response.json()["success"] is True
    assert response.json()["data"]["filename"] == "article.md"


# ── 面经列表响应加固（`_article_out`）──────────────────────────────────
# 背景：`ArticleOut` 把 company / raw_content / source 声明为**必填 str**，
# 而建表时（`models/interview.py`）这三列都是 `nullable=True`；直接
# `model_validate(item)` 一旦碰到 NULL 就抛 ValidationError，**整个列表接口 500、
# 整页数据全没**（桌面端 2026-10-05~10-07 连续 25 次 500 即这一症状）。
#
# ⚠️ 这组用例必须带「反向对照」：当前生产库恰好没有 NULL 行，
# 只写正向用例会恒绿、根本看不出守卫有没有用。

from datetime import datetime  # noqa: E402

import pytest  # noqa: E402
from pydantic import ValidationError  # noqa: E402

from app.modules.interview.routes import ArticleOut, _article_out  # noqa: E402


class _ArticleStub:
    """ORM 行的最小替身，用于构造「DB 可空列取 NULL」的边界行。"""

    def __init__(self, **kw):
        self.id = kw.get("id", "stub-id")
        self.company = kw.get("company", "ByteDance")
        self.position = kw.get("position", "Backend")
        self.raw_content = kw.get("raw_content", "content")
        self.source = kw.get("source", "manual")
        self.questions = kw.get("questions", [])
        self.tags = kw.get("tags", None)
        self.difficulty = kw.get("difficulty", None)
        self.created_at = kw.get("created_at", datetime(2026, 10, 8, 12, 0, 0))


def test_article_out_passes_clean_row():
    """正向：正常行走 `_article_out`，字段原样带出。"""
    out = _article_out(_ArticleStub())
    assert out.company == "ByteDance"
    assert out.position == "Backend"
    assert out.raw_content == "content"
    assert out.source == "manual"
    assert out.created_at is not None


def test_article_out_guard_is_actually_needed():
    """反向对照：证明「不加守卫」时 NULL 行确实会打挂序列化。

    这条用例的作用是**证明缺陷类真实存在**——否则加固就成了
    「没法验证有没有用的改动」。断言方向：必须抛 ValidationError。
    """
    bad = _ArticleStub(company=None, raw_content=None, source=None)
    with pytest.raises(ValidationError):
        ArticleOut.model_validate(bad)


def test_article_out_degrades_null_strings():
    """NULL 的 company / raw_content / source 降级为空串与 unknown，不再抛异常。"""
    out = _article_out(_ArticleStub(company=None, raw_content=None, source=None))
    assert out.company == ""
    assert out.raw_content == ""
    assert out.source == "unknown"


def test_article_out_accepts_null_created_at():
    """created_at 为 NULL 时不再炸（该列只有 server_default、无 NOT NULL 约束）。"""
    out = _article_out(_ArticleStub(created_at=None))
    assert out.created_at is None


def test_article_out_degrades_non_list_json_columns():
    """questions / tags 存成非 list（如对象）时降级为 None，而不是 500。"""
    out = _article_out(_ArticleStub(questions={"q": 1}, tags={"t": 1}))
    assert out.questions is None
    assert out.tags is None


def test_articles_endpoint_survives_null_row(client):
    """端到端：库里真存在一行 NULL 面经时，列表接口仍返回 200 且该行被降级带出。

    注入方式：从 `client` 夹具注册的 get_db 覆盖里取同一个 session，直接绕过
    请求模型写入 NULL（正常 API 走不进来——POST 的 company 是必填 min_length=1）。
    """
    from app.core.database import get_db
    from app.main import app

    session = next(app.dependency_overrides[get_db]())
    try:
        session.add(InterviewArticle(
            id="null-row-1", user_id="default",
            company=None, position=None, raw_content=None, source=None,
        ))
        session.commit()
    finally:
        session.close()

    resp = client.get("/api/v1/interview/articles?page=1&page_size=100")
    assert resp.status_code == 200, resp.text
    items = resp.json()["data"]["items"]
    row = next(i for i in items if i["id"] == "null-row-1")
    assert row["company"] == ""
    assert row["raw_content"] == ""
    assert row["source"] == "unknown"


def test_articles_endpoint_requires_auth(auth_client):
    """反向对照：同一端点去掉登录态必须 401（证明上面的 200 不是因为鉴权被绕过）。"""
    resp = auth_client.get("/api/v1/interview/articles?page=1&page_size=20")
    assert resp.status_code == 401

