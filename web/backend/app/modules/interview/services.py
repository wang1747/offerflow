import logging
import json
import re
from typing import Optional, Tuple
from sqlalchemy.orm import Session
from sqlalchemy import desc

from app.models.interview import InterviewArticle, InterviewQuestion, InterviewSession
from app.models.resume import Resume
from app.models.jd import JobDescription
from app.models.user import User
from app.agents.graphs.mock_interview import (
    generate_question,
    evaluate_answer,
    generate_interview_summary,
)
from app.core.llm import get_user_llm_or_raise
from app.core.prompt_guards import INJECTION_GUARD, attach
from app.core.text_similarity import dedup_normalize, is_dup_pair
from app.observability.tracing import trace_operation

logger = logging.getLogger(__name__)

_ARTICLE_SORT_FIELDS = {"created_at", "company", "position"}


def _find_duplicate(raw_content: str, db: Session, user_id: str) -> Optional[InterviewArticle]:
    """按规范化文本相似度查找重复面经（相似度 >= 0.9 视为重复）。

    两路判据（见 `core.text_similarity.is_dup_pair`），旧版只有第一路：
    1. 整段相似度 ≥ 0.9；
    2. **分段指纹包含度 ≥ 0.9** —— 一份长面经被补了几段再导入，整段相似度会掉到
       0.9 以下而漏判「重复导入同一份面经」；指纹按「较短一方的格子被覆盖多少」算，
       补内容不影响它。

    另外旧版的规范化只 `re.sub(r"\\s+", "", ...)`：全角逗号 / 句号 / 括号都留着，
    同一份面经把 `，` 改成 `,` 就能绕过查重。这里统一改用 `dedup_normalize`
    （全半角归一 + 去标点 + 转小写）。
    """
    normalized = dedup_normalize(raw_content)
    if not normalized:
        return None
    articles = db.query(InterviewArticle).filter(
        InterviewArticle.user_id == user_id
    ).all()
    for article in articles:
        if is_dup_pair(normalized, dedup_normalize(article.raw_content)):
            return article
    return None


def import_article(
    company: str,
    raw_content: str,
    db: Session,
    user_id: str,
    position: Optional[str] = None,
    source: str = "manual",
    client_id: Optional[str] = None
) -> Tuple[InterviewArticle, bool]:
    """导入面经文章；返回 (文章, 是否重复)

    `client_id`：桌面版离线新建时自己生成的 id，联网后重放导入请求。
    **命中已存在的 (id, user_id) 时直接返回、不再走重复检测与后续 LLM 抽取** ——
    幂等重放不该产生第二次模型费用。
    """
    if client_id:
        existed = db.query(InterviewArticle).filter(
            InterviewArticle.id == client_id,
            InterviewArticle.user_id == user_id,
        ).first()
        if existed:
            return existed, False

    duplicate = _find_duplicate(raw_content, db, user_id)
    if duplicate:
        return duplicate, True

    article = InterviewArticle(
        user_id=user_id,
        company=company,
        position=position,
        raw_content=raw_content,
        source=source,
    )
    if client_id:
        article.id = client_id
    db.add(article)
    db.commit()
    db.refresh(article)

    # 贡献语料：用户同意才把面经写入共享语料库（默认关闭）
    from app.models.user import User as _User
    owner = db.query(_User).filter(_User.id == user_id).first()
    if owner and owner.allow_corpus:
        try:
            from app.modules.corpus.services import add_corpus_item
            add_corpus_item(
                item_type="interview",
                raw_text=raw_content,
                db=db,
                structured={"company": company, "position": position},
                source="user_upload",
                user_id=user_id,
                is_public=True,
                source_id=article.id,
            )
        except Exception as e:  # noqa: BLE001
            print(f"[WARN] 面经贡献语料失败: {e}")

    return article, False


def list_articles(
    db: Session,
    user_id: str,
    page: int = 1,
    page_size: int = 20,
    company: Optional[str] = None,
    position: Optional[str] = None,
    sort_by: str = "created_at",
    sort_order: str = "desc"
) -> Tuple[list, int]:
    """获取面经列表（带分页和筛选）"""
    query = db.query(InterviewArticle).filter(
        InterviewArticle.user_id == user_id
    )

    if company:
        query = query.filter(InterviewArticle.company.ilike(f"%{company}%"))
    if position:
        query = query.filter(InterviewArticle.position.ilike(f"%{position}%"))

    if sort_by not in _ARTICLE_SORT_FIELDS:
        sort_by = "created_at"
    if sort_order not in ("asc", "desc"):
        sort_order = "desc"

    if sort_order == "desc":
        query = query.order_by(desc(getattr(InterviewArticle, sort_by)))
    else:
        query = query.order_by(getattr(InterviewArticle, sort_by))

    total = query.count()
    items = query.offset((page - 1) * page_size).limit(page_size).all()
    return items, total


def delete_article(article_id: str, db: Session, user_id: str) -> bool:
    """删除面经"""
    from app.modules.corpus.services import delete_corpus_by_source
    article = db.query(InterviewArticle).filter(
        InterviewArticle.id == article_id,
        InterviewArticle.user_id == user_id
    ).first()
    if not article:
        return False
    db.query(InterviewQuestion).filter(
        InterviewQuestion.article_id == article_id,
        InterviewQuestion.user_id == user_id
    ).delete(synchronize_session=False)
    db.delete(article)
    db.commit()
    # 级联清理该面经贡献到共享语料库的副本
    delete_corpus_by_source(article_id, item_type="interview", db=db)
    return True


def get_questions(
    db: Session,
    user_id: str,
    article_id: Optional[str] = None,
    page: int = 1,
    page_size: int = 20
) -> Tuple[list, int]:
    """获取面试题列表（支持按面经筛选）"""
    query = db.query(InterviewQuestion).filter(
        InterviewQuestion.user_id == user_id
    )

    if article_id:
        query = query.filter(InterviewQuestion.article_id == article_id)

    total = query.count()
    items = query.offset((page - 1) * page_size).limit(page_size).all()
    return items, total


def extract_questions_from_article(article_id: str, db: Session, user_id: str) -> list:
    """把面经中已提取的题目落库为面试题记录"""
    article = db.query(InterviewArticle).filter(
        InterviewArticle.id == article_id,
        InterviewArticle.user_id == user_id
    ).first()
    if not article or not article.questions:
        return []

    saved = []
    for item in article.questions:
        if isinstance(item, str):
            question_text = item
            category = None
        else:
            question_text = item.get("question", "") if isinstance(item, dict) else ""
            category = item.get("category") if isinstance(item, dict) else None
        if not question_text:
            continue
        exists = db.query(InterviewQuestion).filter(
            InterviewQuestion.user_id == user_id,
            InterviewQuestion.question == question_text
        ).first()
        if exists:
            continue
        question = InterviewQuestion(
            user_id=user_id,
            article_id=article_id,
            question=question_text,
            category=category,
            difficulty=article.difficulty,
        )
        db.add(question)
        saved.append(question)
    db.commit()
    return saved


def _clean_json_response(raw: str) -> str:
    """清洗 LLM 输出的 JSON 字符串"""
    raw = re.sub(r"^```json\s*", "", raw.strip())
    raw = re.sub(r"^```\s*", "", raw)
    raw = re.sub(r"```$", "", raw)
    return raw.strip()


def extract_article_metadata(raw_content: str, user: User) -> dict:
    """用 LLM 从面经中提取题目、标签和难度；失败时返回空结果"""
    from langchain_core.messages import HumanMessage, SystemMessage

    # 输入是用户上传的面经原文 ⇒ 挂防注入（只做提取、不打分、不产出简历正文）。
    system_prompt = attach(
        "你是面经整理助手。从面经内容中提取结构化信息，只输出 JSON：\n"
        '{"questions": ["面试问题1", "面试问题2"], "tags": ["算法", "系统设计"], "difficulty": "简单|中等|困难"}\n'
        "提取不到时用空数组，难度无法判断时为 null。",
        INJECTION_GUARD,
    )
    try:
        llm = get_user_llm_or_raise(user)
        with trace_operation("article_extract", getattr(user, "id", None)):
            response = llm.invoke([
                SystemMessage(content=system_prompt),
                HumanMessage(content=raw_content or ""),
            ])
        parsed = json.loads(_clean_json_response(response.content))
        return {
            "questions": parsed.get("questions", []) if isinstance(parsed.get("questions"), list) else [],
            "tags": parsed.get("tags", []) if isinstance(parsed.get("tags"), list) else [],
            "difficulty": parsed.get("difficulty"),
        }
    except ValueError as e:
        logger.warning(f"提取面经元数据失败（用户未配置LLM）: {e}")
        return {"questions": [], "tags": [], "difficulty": None}
    except Exception as e:
        logger.error(f"提取面经元数据异常: {e}")
        return {"questions": [], "tags": [], "difficulty": None}


def save_generated_questions(
    user_id: str,
    article_id: Optional[str],
    questions: list,
    db: Session
) -> list:
    """持久化生成的面试题"""
    saved = []
    for item in questions:
        question = InterviewQuestion(
            user_id=user_id,
            article_id=article_id,
            question=item["question"],
            answer=item.get("answer"),
            category=item.get("category"),
            difficulty=item.get("difficulty"),
        )
        db.add(question)
        saved.append(question)
    db.commit()
    return saved


# ============ 模拟面试会话功能 ============

def create_interview_session(
    resume_id: str,
    jd_id: str,
    db: Session,
    user_id: str,
    user: User
) -> InterviewSession:
    """创建模拟面试会话"""
    resume = db.query(Resume).filter(
        Resume.id == resume_id,
        Resume.user_id == user_id
    ).first()
    if not resume:
        raise ValueError("简历不存在")
    
    jd = db.query(JobDescription).filter(
        JobDescription.id == jd_id,
        JobDescription.user_id == user_id
    ).first()
    if not jd:
        raise ValueError("JD不存在")
    
    session = InterviewSession(
        user_id=user_id,
        resume_id=resume_id,
        jd_id=jd_id,
        status="active",
        questions=[],
        answers=[],
        feedbacks=[],
        current_question=""
    )
    db.add(session)
    db.commit()
    db.refresh(session)
    
    first_question = generate_question(
        resume_text=resume.raw_text,
        jd_text=jd.raw_text,
        user=user
    )
    session.current_question = first_question
    session.questions = [first_question]
    db.commit()
    
    return session


def get_interview_session(session_id: str, db: Session, user_id: str) -> Optional[InterviewSession]:
    """获取会话"""
    return db.query(InterviewSession).filter(
        InterviewSession.id == session_id,
        InterviewSession.user_id == user_id
    ).first()


def finish_interview_session(session_id: str, db: Session, user_id: str) -> bool:
    """提前结束模拟面试（用户主动结束）。"""
    session = get_interview_session(session_id, db, user_id)
    if not session:
        return False
    session.status = "finished"
    db.commit()
    return True


def submit_interview_answer(
    session_id: str,
    answer: str,
    db: Session,
    user_id: str,
    user: User
) -> Tuple[Optional[str], Optional[str], bool]:
    """
    提交回答，返回反馈和下一题
    """
    session = db.query(InterviewSession).filter(
        InterviewSession.id == session_id,
        InterviewSession.user_id == user_id
    ).first()
    
    if not session:
        return None, None, False
    
    if session.status == "finished":
        return None, None, True
    
    resume = db.query(Resume).filter(
        Resume.id == session.resume_id,
        Resume.user_id == user_id
    ).first()
    jd = db.query(JobDescription).filter(
        JobDescription.id == session.jd_id,
        JobDescription.user_id == user_id
    ).first()
    if not resume or not jd:
        raise ValueError("会话关联的简历或JD不存在")
    
    current_answers = list(session.answers or [])
    current_answers.append(answer)
    session.answers = current_answers
    
    feedback = evaluate_answer(
        question=session.current_question,
        answer=answer,
        user=user
    )
    current_feedbacks = list(session.feedbacks or [])
    current_feedbacks.append(feedback)
    session.feedbacks = current_feedbacks
    
    question_count = len(session.questions or [])
    if question_count >= 8:
        session.status = "finished"
        db.commit()
        return feedback, None, True
    
    next_question = generate_question(
        resume_text=resume.raw_text,
        jd_text=jd.raw_text,
        user=user,
        previous_question=session.current_question,
        previous_answer=answer,
        asked_questions=list(session.questions or []),   # 整场已问过的题，避免重复考点
    )
    current_questions = list(session.questions or [])
    current_questions.append(next_question)
    session.questions = current_questions
    session.current_question = next_question
    
    db.commit()
    return feedback, next_question, False


def get_interview_summary(session_id: str, db: Session, user_id: str, user: User) -> Optional[dict]:
    """获取面试总结"""
    session = get_interview_session(session_id, db, user_id)
    if not session:
        return None
    
    summary_text = ""
    if session.answers:
        summary_text = generate_interview_summary(
            questions=session.questions or [],
            answers=session.answers or [],
            feedbacks=session.feedbacks or [],
            user=user
        )

    return {
        "summary": summary_text,
        "total_questions": len(session.questions or []),
        "questions": session.questions,
        "answers": session.answers,
        "feedbacks": session.feedbacks,
        "status": session.status,
        "created_at": session.created_at
    }