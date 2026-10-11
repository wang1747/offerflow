import json
import os
import re
import sys

import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.core.database import Base, get_db
from app.main import app
from app.models.user import User


class FakeLLM:
    """离线测试用的 LLM，按 prompt 关键字返回对应 JSON

    两个可注入钩子（测试里给属性赋值即可定制行为）：
    - ``condense_handler``: ``callable(text) -> (optimized_text, removed)``，
      驱动"三刀精简"；默认原样返回（模拟"LLM 没能精简"）。
    - ``extract_handler``: ``callable(text) -> dict | None``，
      驱动"从纯文本提取结构化"；默认返回一份最小可用结构。
    - ``regenerate_handler``: ``callable(current_structured) -> dict``，
      驱动"逐段重写"的返回体。
      **必须能模拟"模型只回被改的那一段"**：默认行为是 ``dict(current)`` 完美回吐，
      这会让"模型漏字段"的缺陷在单测里永远绿（生产就是这么翻车的）。
    """

    def __init__(self):
        self.condense_handler = None
        self.extract_handler = None
        self.regenerate_handler = None

    def invoke(self, messages):
        content = "\n".join(getattr(m, "content", "") or "" for m in messages)
        human_content = getattr(messages[-1], "content", "") if messages else ""
        # 判别串取自 jd_analysis 的真实 system prompt；prompt 改动时这里要同步，
        # 否则会悄悄走兜底分支（曾导致 test_jd_parse_api 报 KeyError: 'company'）。
        if "JD parser" in content or "JD（职位描述）解析器" in content:
            payload = {
                "company": "ByteDance",
                "position": "Backend Engineer",
                "must_have": ["Python", "本科"],
                "nice_to_have": ["Docker"],
                "tech_stack": {
                    "backend": ["Python", "FastAPI"],
                    "frontend": [],
                    "infra": ["Docker"],
                    "other": [],
                },
                "hidden_signals": ["团队扩张快"],
            }
        elif "简历优化专家" in content:
            m = re.search(
                r"【原始简历】\n(.*?)\n\n【目标岗位结构化需求】",
                content,
                re.DOTALL,
            )
            resume = m.group(1).strip() if m else "Test Resume"
            payload = {
                "optimized_text": resume,
                "changes": [],
                "added_keywords": [],
                "removed_keywords": [],
            }
        elif "资深简历撰写专家" in content:
            payload = {
                "name": "张三",
                "position": "Python 后端实习",
                "contact": {
                    "phone": "13800138000",
                    "email": "zhangsan@example.com",
                    "github": "",
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
                        "bullets": ["使用 FastAPI 搭建后端，支撑日均 1000 次调用"],
                    }
                ],
                "skills": ["Python", "FastAPI"],
                "certifications": [],
                "tips": ["补充量化成果"],
            }
        elif "简历精简专家" in content:
            # 三刀裁剪：默认"什么都没删"（模拟 LLM 没给出有效精简）
            if callable(self.condense_handler):
                optimized, removed = self.condense_handler(human_content)
            else:
                optimized, removed = human_content, []
            payload = {"optimized_text": optimized, "removed": list(removed or [])}
        elif "简历解析专家" in content:
            # 从纯文本提取结构化：默认给一份最小可用结构（供"导出不写库"的测试用）
            if callable(self.extract_handler):
                extracted = self.extract_handler(human_content)
                return AIMessage(content=json.dumps(extracted, ensure_ascii=False))
            payload = {
                "name": "提取姓名",
                "position": "提取意向",
                "contact": {"phone": "", "email": "", "github": ""},
                "summary": "从纯文本提取出来的总结",
                "education": [
                    {
                        "school": "提取学校",
                        "major": "提取专业",
                        "degree": "本科",
                        "start": "2023-09",
                        "end": "2027-06",
                        "detail": "",
                    }
                ],
                "experiences": [
                    {
                        "type": "project",
                        "name": "提取项目",
                        "role": "开发",
                        "start": "",
                        "end": "",
                        "bullets": ["提取出来的要点"],
                    }
                ],
                "skills": ["Python"],
                "certifications": [],
            }
        elif "请只重新生成" in content:
            try:
                current = json.loads(human_content).get("current") or {}
            except Exception:
                current = {}
            if callable(self.regenerate_handler):
                payload = dict(self.regenerate_handler(current))
            else:
                payload = dict(current)
                payload["tips"] = list(payload.get("tips") or []) + ["已重新生成"]
        elif "面试题生成专家" in content:
            payload = {
                "questions": [
                    {"question": "讲一下你最有挑战的项目", "category": "项目", "difficulty": "中等"}
                ]
            }
        elif "资深面试官" in content:
            return AIMessage(content="整体表现不错，注意结构化表达。")
        elif "专业的面试官" in content:
            return AIMessage(content="请介绍一下你的项目经验。")
        else:
            payload = {"questions": [], "tags": [], "difficulty": None}
        return AIMessage(content=json.dumps(payload, ensure_ascii=False))


@pytest.fixture(autouse=True)
def _fake_llm(monkeypatch):
    """所有测试使用离线 LLM，避免外网依赖"""
    fake = FakeLLM()

    import app.agents.graphs.interview_prep as interview_prep
    import app.agents.graphs.jd_analysis as jd_analysis
    import app.agents.graphs.mock_interview as mock_interview
    import app.modules.interview.services as interview_service
    import app.modules.resume.optimizer.service as optimizer_service
    import app.modules.resume.optimizer.structure_extract as structure_extract
    import app.modules.resume_generation.services as resume_generation_service

    monkeypatch.setattr(jd_analysis, "get_user_llm_or_raise", lambda user: fake)
    monkeypatch.setattr(optimizer_service, "get_user_llm_or_raise", lambda user: fake)
    # 导出前的惰性结构化提取：不 patch 的话它永远走"未配置模型"的降级分支，
    # 等于导出这条路的结构化分支从来没被测试覆盖过
    monkeypatch.setattr(structure_extract, "get_user_llm_or_raise", lambda user: fake)
    monkeypatch.setattr(interview_prep, "get_user_llm_or_raise", lambda user: fake)
    monkeypatch.setattr(mock_interview, "get_user_llm_or_raise", lambda user: fake)
    monkeypatch.setattr(interview_service, "get_user_llm_or_raise", lambda user: fake)
    monkeypatch.setattr(resume_generation_service, "get_user_llm_or_raise", lambda user: fake)
    return fake


@pytest.fixture
def assert_protocol_clean():
    """断言"**真正发给模型**的那份消息"满足 provider 协议。

    2026-10-04 生产冒烟抓到的缺陷：``select_read_tools`` 直接发 ``state["messages"]``，
    而 ``execute_read_tools`` 的结果从不写回 messages ⇒ 历史里留下"带 ``tool_calls``
    却没有配对 tool 消息"的孤儿 AIMessage，同一会话**第二条消息起必 400**。
    假模型不校验协议，所以旧用例即使全绿也抓不住它。

    判据只能落在消息**结构**上（不能靠"模型报没报错"）：
    1. ``SystemMessage`` 只能在第 0 位（插在对话中间同样会被部分 provider 拒绝）；
    2. 每个带 ``tool_calls`` 的 ``AIMessage`` 之后必须紧跟每个 ``tool_call_id``
       对应的 ``ToolMessage``（OpenAI/DeepSeek 协议硬要求）。
    3. ``messages`` 里不得出现非消息对象（例如把 dict 直接塞进去）。

    ⚠️ 第一条用例必须能做**反向对照**：把缺陷放回去要变红，否则"全绿"与"没测到"分不清。
    """
    from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

    _LEGAL = (SystemMessage, HumanMessage, AIMessage, ToolMessage)

    def _check(messages, label=""):
        where = f"（{label}）" if label else ""
        messages = list(messages)
        assert messages, f"{where}请求里一条消息都没有"

        for i, m in enumerate(messages):
            assert isinstance(m, _LEGAL), (
                f"{where}第 {i} 条不是消息对象，而是 {type(m).__name__}"
                "（把 dict 直接塞进请求会 400）"
            )

        # 先查"孤儿 tool_calls"：这是真实缺陷的根因，报出来的信息最准
        for i, m in enumerate(messages):
            if not isinstance(m, AIMessage):
                continue
            tcs = getattr(m, "tool_calls", None) or []
            if not tcs:
                continue
            want = [tc.get("id") for tc in tcs]
            following = messages[i + 1: i + 1 + len(want)]
            assert following, (
                f"{where}第 {i} 条 AIMessage 带 tool_calls {want}，后面却一条消息都没有——"
                "这正是「带 tool_calls 却没有配对 tool 消息」的孤儿形态，provider 会直接 400"
            )
            got = []
            for nxt in following:
                assert isinstance(nxt, ToolMessage), (
                    f"{where}第 {i} 条 AIMessage 带 tool_calls {want}，"
                    f"紧随其后的却是 {type(nxt).__name__}，不是 tool 消息"
                    "——provider 会直接 400（说明把不该带的历史续用上了）"
                )
                got.append(getattr(nxt, "tool_call_id", None))
            assert got == want, (
                f"{where}tool_call_id 不配对：{got} != {want}（provider 会直接 400）"
            )

        for i, m in enumerate(messages):
            if isinstance(m, SystemMessage) and i != 0:
                raise AssertionError(
                    f"{where}System 消息出现在第 {i} 位；System 只能作为首条，"
                    "否则说明把历史当日志续用了"
                )

    return _check


@pytest.fixture
def db_session():
    """内存 SQLite 数据库测试夹具"""
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    TestSession = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    session = TestSession()
    session.add(User(id="default", name="默认用户"))
    session.commit()
    try:
        yield session
    finally:
        session.close()
        Base.metadata.drop_all(bind=engine)


@pytest.fixture
def auth_client(tmp_path):
    """FastAPI TestClient 实例：临时文件数据库 + 离线 LLM，线程安全"""
    from app.config import get_settings
    settings = get_settings()
    settings.chroma_persist_path = str(tmp_path / "chroma")

    db_path = tmp_path / "test.db"
    engine = create_engine(
        f"sqlite:///{db_path.as_posix()}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(bind=engine)
    TestingSession = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    with TestingSession() as session:
        session.add(User(id="default", name="默认用户"))
        session.commit()

    def override_get_db():
        db = TestingSession()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()
    engine.dispose()


@pytest.fixture
def client(auth_client):
    from app.modules.auth.routes import get_current_user_required

    def override_current_user():
        return User(id="default", name="默认用户")

    app.dependency_overrides[get_current_user_required] = override_current_user
    yield auth_client
    app.dependency_overrides.clear()


@pytest.fixture
def sample_jd_text() -> str:
    return "ByteDance hiring Python backend engineer, bachelor's degree required, 3+ years Python experience."


@pytest.fixture
def sample_resume_text() -> str:
    return "John Doe, CS Master, 5 years Python experience, familiar with Django and FastAPI."
