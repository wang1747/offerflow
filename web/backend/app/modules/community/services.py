"""社区分享服务：帖子 CRUD + 评论 + 点赞。"""
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

from app.models.community import CommunityComment, CommunityLike, CommunityPost
from app.models.user import User

_POST_CATEGORIES = ["经验分享", "面经", "offer分享", "资源推荐", "其他"]


def create_post(db: Session, user_id: str, title: str, content: str,
                category: str = "经验分享", tags: Optional[List[str]] = None,
                attachments: Optional[List[Dict[str, Any]]] = None) -> CommunityPost:
    if category not in _POST_CATEGORIES:
        category = "其他"
    post = CommunityPost(
        user_id=user_id,
        title=title.strip(),
        content=content.strip(),
        category=category,
        tags=list(tags) if tags else [],
        attachments=list(attachments) if attachments else None,
    )
    db.add(post)
    db.commit()
    db.refresh(post)
    return post


def _post_out(post: CommunityPost) -> Dict[str, Any]:
    return {
        "id": post.id,
        "user_id": post.user_id,
        "author": None,  # 由调用方批量填充（避免 N+1）
        "title": post.title,
        "content": post.content,
        "category": post.category,
        "tags": post.tags or [],
        "attachments": post.attachments or [],
        "like_count": post.like_count or 0,
        "comment_count": post.comment_count or 0,
        "created_at": str(post.created_at) if post.created_at else None,
    }


def list_posts(db: Session, category: Optional[str] = None,
               page: int = 1, page_size: int = 20) -> Dict[str, Any]:
    q = db.query(CommunityPost)
    if category:
        q = q.filter(CommunityPost.category == category)
    total = q.count()
    posts = (q.order_by(CommunityPost.created_at.desc())
             .offset((page - 1) * page_size).limit(page_size).all())
    # 批量取作者名与头像，避免 N+1
    user_ids = {p.user_id for p in posts}
    users = {u.id: u for u in db.query(User).filter(User.id.in_(user_ids)).all()}
    data = []
    for p in posts:
        item = _post_out(p)
        u = users.get(p.user_id)
        item["author"] = u.name if u else "匿名用户"
        item["author_avatar"] = u.avatar if u else None
        data.append(item)
    return {"items": data, "total": total, "page": page, "page_size": page_size}


def get_post(db: Session, post_id: str) -> Optional[CommunityPost]:
    return db.query(CommunityPost).filter(CommunityPost.id == post_id).first()


def get_post_detail(db: Session, post_id: str) -> Optional[Dict[str, Any]]:
    post = get_post(db, post_id)
    if not post:
        return None
    item = _post_out(post)
    comments = (db.query(CommunityComment)
                .filter(CommunityComment.post_id == post_id)
                .order_by(CommunityComment.created_at.asc()).all())
    uid = {c.user_id for c in comments} | {post.user_id}
    users = {u.id: u for u in db.query(User).filter(User.id.in_(uid)).all()}
    pu = users.get(post.user_id)
    item["author"] = pu.name if pu else "匿名用户"
    item["author_avatar"] = pu.avatar if pu else None
    item["comments"] = [{
        "id": c.id,
        "user_id": c.user_id,
        "author": (users.get(c.user_id).name if users.get(c.user_id) else "匿名用户"),
        "author_avatar": (users.get(c.user_id).avatar if users.get(c.user_id) else None),
        "content": c.content,
        "created_at": str(c.created_at) if c.created_at else None,
    } for c in comments]
    return item


def add_comment(db: Session, post_id: str, user_id: str, content: str) -> CommunityComment:
    post = get_post(db, post_id)
    if not post:
        raise ValueError("帖子不存在")
    comment = CommunityComment(post_id=post_id, user_id=user_id, content=content.strip())
    post.comment_count = (post.comment_count or 0) + 1
    db.add(comment)
    db.commit()
    db.refresh(comment)
    return comment


def toggle_like(db: Session, post_id: str, user_id: str) -> Dict[str, Any]:
    post = get_post(db, post_id)
    if not post:
        raise ValueError("帖子不存在")
    existing = (db.query(CommunityLike)
                .filter(CommunityLike.post_id == post_id, CommunityLike.user_id == user_id)
                .first())
    if existing:
        db.delete(existing)
        post.like_count = max(0, (post.like_count or 0) - 1)
        liked = False
    else:
        db.add(CommunityLike(post_id=post_id, user_id=user_id))
        post.like_count = (post.like_count or 0) + 1
        liked = True
    db.commit()
    return {"liked": liked, "like_count": post.like_count or 0}


def delete_post(db: Session, post_id: str, user_id: str, is_admin: bool = False) -> bool:
    post = get_post(db, post_id)
    if not post:
        return False
    if not is_admin and post.user_id != user_id:
        raise PermissionError("无权删除他人帖子")
    db.delete(post)
    db.commit()
    return True


def delete_comment(db: Session, comment_id: str, is_admin: bool = False) -> bool:
    """删除评论（管理员治理入口）。同步扣减帖子评论数。"""
    comment = (db.query(CommunityComment)
               .filter(CommunityComment.id == comment_id).first())
    if not comment:
        return False
    if not is_admin:
        raise PermissionError("无权删除他人评论")
    post = get_post(db, comment.post_id)
    db.delete(comment)
    if post:
        post.comment_count = max(0, (post.comment_count or 0) - 1)
    db.commit()
    return True
