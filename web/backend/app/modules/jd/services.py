from typing import Optional

from sqlalchemy.orm import Session

from app.models.jd import JobDescription
from app.models.user import User
from app.models.application import Application
from app.models.match import MatchResult
from app.agents.graphs.jd_analysis import analyze_jd


def _parsed_from_row(jd: JobDescription) -> dict:
    """把库里的 JD 还原成 analyze_jd 输出的 parsed 结构（幂等重放时用）。"""
    return {
        "company": jd.company,
        "position": jd.position,
        "must_have": jd.must_have or [],
        "nice_to_have": jd.nice_to_have or [],
        "tech_stack": jd.tech_stack or {},
        "hidden_signals": jd.hidden_signals or [],
    }


async def parse_and_save(raw_text: str, db: Session, user_id: str,
                         client_id: Optional[str] = None) -> dict:
    """解析并保存 JD。

    `client_id`：桌面版离线新建时会自己生成 id，联网后重放这次解析请求。
    **命中已存在的 (id, user_id) 时直接返回、不再调 LLM** —— 幂等重放不该产生
    第二次模型费用（这是「客户端传 id」与「重复上行」并存的前提）。
    """
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        return {"success": False, "data": None, "error": "用户不存在"}

    if client_id:
        existed = db.query(JobDescription).filter(
            JobDescription.id == client_id,
            JobDescription.user_id == user_id,
        ).first()
        if existed:
            return {"success": True,
                    "data": {"id": existed.id, "parsed": _parsed_from_row(existed)},
                    "error": None}

    result = await analyze_jd(raw_text, user)
    if result.get("error"):
        return {"success": False, "data": None, "error": result["error"]}

    parsed = result.get("parsed", {})
    fields = {
        "user_id": user_id,
        "raw_text": raw_text,
        "company": parsed.get("company"),
        "position": parsed.get("position"),
        "must_have": parsed.get("must_have"),
        "nice_to_have": parsed.get("nice_to_have"),
        "tech_stack": parsed.get("tech_stack"),
        "hidden_signals": parsed.get("hidden_signals"),
    }
    jd = JobDescription(**fields)
    if client_id:
        jd.id = client_id
    db.add(jd)
    db.commit()
    db.refresh(jd)

    # 贡献语料：用户已同意「贡献语料」才写入共享语料库（默认关闭）
    if user.allow_corpus:
        try:
            from app.modules.corpus.services import add_corpus_item
            add_corpus_item(
                item_type="jd",
                raw_text=raw_text,
                db=db,
                structured=parsed,
                source="user_upload",
                user_id=user_id,
                is_public=True,
                source_id=jd.id,
            )
        except Exception as e:  # noqa: BLE001
            print(f"[WARN] JD 贡献语料失败: {e}")

    return {"success": True, "data": {"id": jd.id, "parsed": parsed}, "error": None}


def list_jds(db: Session, user_id: str) -> list:
    return db.query(JobDescription).filter(
        JobDescription.user_id == user_id
    ).order_by(JobDescription.created_at.desc()).all()


def delete_jd(jd_id: str, db: Session, user_id: str) -> bool:
    from app.modules.corpus.services import delete_corpus_by_source
    jd = db.query(JobDescription).filter(
        JobDescription.id == jd_id,
        JobDescription.user_id == user_id
    ).first()
    if not jd:
        return False
    # 级联清理：删除关联的匹配结果，投递记录解除 JD 关联（避免孤儿数据）
    db.query(MatchResult).filter(
        MatchResult.jd_id == jd_id
    ).delete(synchronize_session=False)
    db.query(Application).filter(
        Application.jd_id == jd_id,
        Application.user_id == user_id,
    ).update({"jd_id": None}, synchronize_session=False)
    db.delete(jd)
    db.commit()
    # 级联清理该 JD 贡献到共享语料库的副本
    delete_corpus_by_source(jd_id, item_type="jd", db=db)
    return True


def update_jd(
    jd_id: str,
    db: Session,
    user_id: str,
    company: Optional[str] = None,
    position: Optional[str] = None,
):
    jd = db.query(JobDescription).filter(
        JobDescription.id == jd_id,
        JobDescription.user_id == user_id
    ).first()
    if not jd:
        return None
    if company is not None:
        jd.company = company.strip() or None
    if position is not None:
        jd.position = position.strip() or None
    db.commit()
    db.refresh(jd)
    return jd
