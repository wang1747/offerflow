"""管理员后台：统计查询、系统设置、配额校验

时间说明：数据库 created_at 存的是 **UTC（naive）**，因此所有时间比较统一用 _utcnow()，
不要用 datetime.now()（本地时区会差 8 小时导致「今日」统计错位）。
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models.application import Application
from app.models.audit import AdminAuditLog
from app.models.community import CommunityComment, CommunityPost
from app.models.corpus import CorpusItem
from app.models.feedback import Feedback
from app.models.interview import InterviewSession
from app.models.jd import JobDescription
from app.models.resume import Resume
from app.models.setting import SETTING_DEFS, SystemSetting
from app.models.trace import Span, Trace
from app.models.user import User
from app.observability.cost import usd_to_cny

logger = logging.getLogger(__name__)


def _utcnow() -> datetime:
    """与数据库一致的 UTC naive 时间"""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _fmt_dt(dt: Optional[datetime]) -> Optional[str]:
    return dt.isoformat() if dt else None


# ============================================================
# 系统设置
# ============================================================

def get_settings_map(db: Session) -> Dict[str, Any]:
    """读取全部设置项（含默认值兜底）"""
    rows = {r.key: r for r in db.query(SystemSetting).all()}
    result: Dict[str, Any] = {}
    for key, default, desc in SETTING_DEFS:
        row = rows.get(key)
        if row is None or row.value is None:
            value = default
        else:
            try:
                value = json.loads(row.value)
            except Exception:  # noqa: BLE001
                value = default
        result[key] = {
            "key": key,
            "value": value,
            "description": desc,
            "updated_by": row.updated_by if row else None,
            "updated_at": _fmt_dt(row.updated_at) if row else None,
        }
    return result


def get_setting_value(db: Session, key: str, default: Any = None) -> Any:
    row = db.query(SystemSetting).filter(SystemSetting.key == key).first()
    if row is None or row.value is None:
        return default
    try:
        return json.loads(row.value)
    except Exception:  # noqa: BLE001
        return default


def update_setting(db: Session, key: str, value: Any, operator_id: str) -> Dict[str, Any]:
    """写入设置项；返回 {old, new}"""
    defaults = {k: v for k, v, _ in SETTING_DEFS}
    if key not in defaults:
        raise ValueError(f"未知设置项: {key}")

    # 旧值要用定义里的默认值兜底，否则首次修改时 old 会是 None
    old = get_setting_value(db, key, defaults[key])
    row = db.query(SystemSetting).filter(SystemSetting.key == key).first()
    payload = json.dumps(value, ensure_ascii=False)
    if row is None:
        desc = next((d for k, _, d in SETTING_DEFS if k == key), None)
        db.add(SystemSetting(key=key, value=payload, description=desc, updated_by=operator_id))
    else:
        row.value = payload
        row.updated_by = operator_id
    db.commit()
    return {"old": old, "new": value}


# ============================================================
# 用量与配额
# ============================================================

def get_daily_tokens_used(db: Session, user_id: str, day: Optional[datetime] = None) -> int:
    """某用户「今日（UTC）」消耗的 token 总数"""
    day = day or _utcnow()
    start = day.replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=1)
    total = (
        db.query(func.coalesce(func.sum(Trace.total_tokens), 0))
        .filter(Trace.user_id == user_id, Trace.created_at >= start, Trace.created_at < end)
        .scalar()
    )
    return int(total or 0)


def check_quota(db: Session, user: User) -> Optional[str]:
    """
    配额校验：仅对「使用平台默认 Key」的用户生效（自带 Key 的用户花自己的钱，不受限）。
    返回超限提示信息；可放行时返回 None。
    """
    if getattr(user, "encrypted_api_key", None):
        return None  # BYOK，不限制

    quota = int(get_setting_value(db, "quota_daily_tokens", 0) or 0)
    if quota <= 0:
        return None  # 0 = 不限制

    used = get_daily_tokens_used(db, user.id)
    if used >= quota:
        return (
            f"今日 AI 用量已达平台上限（{used}/{quota} tokens）。"
            "可在「AI 模型设置」中配置自己的 API Key 以继续使用，或明日再试。"
        )
    return None


# ============================================================
# 管理总览
# ============================================================

def _count(db: Session, model, *filters) -> int:
    q = db.query(func.count(model.id))
    for f in filters:
        q = q.filter(f)
    return int(q.scalar() or 0)


def _sum(db: Session, col, *filters) -> float:
    q = db.query(func.coalesce(func.sum(col), 0))
    for f in filters:
        q = q.filter(f)
    return float(q.scalar() or 0)


def get_overview(db: Session, days: int = 30) -> Dict[str, Any]:
    now = _utcnow()
    since_7d = now - timedelta(days=7)
    since_1d = now - timedelta(days=1)
    trend_start = (now - timedelta(days=days - 1)).replace(hour=0, minute=0, second=0, microsecond=0)

    # ---- 用户 ----
    active_users_7d = (
        db.query(func.count(func.distinct(Trace.user_id)))
        .filter(Trace.created_at >= since_7d, Trace.user_id.isnot(None))
        .scalar()
    )
    users = {
        "total": _count(db, User),
        "active": _count(db, User, User.is_active.is_(True)),
        "new_7d": _count(db, User, User.created_at >= since_7d),
        "byok": _count(db, User, User.encrypted_api_key.isnot(None)),
        "admins": _count(db, User, User.role == "admin"),
        "contributing": _count(db, User, User.allow_corpus.is_(True)),
        "active_7d": int(active_users_7d or 0),
    }

    # ---- 用量 ----
    calls = _count(db, Trace)
    tokens = int(_sum(db, Trace.total_tokens))
    cost = round(_sum(db, Trace.total_cost), 6)
    failed = _count(db, Trace, Trace.status != "success")
    avg_duration = (
        db.query(func.coalesce(func.avg(Trace.duration_ms), 0)).scalar() or 0
    )
    usage = {
        "calls": calls,
        "tokens": tokens,
        "cost": cost,
        "cost_cny": usd_to_cny(cost),
        "failed": failed,
        "fail_rate": round(failed / calls * 100, 2) if calls else 0.0,
        "avg_duration_ms": int(avg_duration),
    }

    # ---- 业务内容 ----
    content = {
        "resumes": _count(db, Resume),
        "jds": _count(db, JobDescription),
        "applications": _count(db, Application),
        "interview_sessions": _count(db, InterviewSession),
        "corpus": _count(db, CorpusItem),
        "posts": _count(db, CommunityPost),
        "comments": _count(db, CommunityComment),
        "feedbacks": _count(db, Feedback),
    }

    # ---- 近 N 天趋势 ----
    day_col = func.date(Trace.created_at)
    rows = (
        db.query(
            day_col.label("d"),
            func.count(Trace.id),
            func.coalesce(func.sum(Trace.total_cost), 0),
            func.coalesce(func.sum(Trace.total_tokens), 0),
        )
        .filter(Trace.created_at >= trend_start)
        .group_by(day_col)
        .all()
    )
    by_day = {str(r[0]): r for r in rows}
    trend: List[Dict[str, Any]] = []
    for i in range(days):
        d = (trend_start + timedelta(days=i)).date()
        key = str(d)
        r = by_day.get(key)
        trend.append({
            "date": key,
            "calls": int(r[1]) if r else 0,
            "cost": round(float(r[2]), 6) if r else 0.0,
            "cost_cny": usd_to_cny(round(float(r[2]), 6)) if r else 0.0,
            "tokens": int(r[3]) if r else 0,
        })

    # ---- Top 用户 ----
    top_rows = (
        db.query(
            Trace.user_id,
            func.count(Trace.id).label("calls"),
            func.coalesce(func.sum(Trace.total_cost), 0).label("cost"),
            func.coalesce(func.sum(Trace.total_tokens), 0).label("tokens"),
        )
        .filter(Trace.user_id.isnot(None))
        .group_by(Trace.user_id)
        .order_by(func.sum(Trace.total_cost).desc())
        .limit(8)
        .all()
    )
    uids = [r[0] for r in top_rows]
    umap = {u.id: u for u in db.query(User).filter(User.id.in_(uids)).all()} if uids else {}
    top_users = [
        {
            "user_id": r[0],
            "name": umap.get(r[0]).name if umap.get(r[0]) else "(已删除)",
            "email": umap.get(r[0]).email if umap.get(r[0]) else None,
            "byok": bool(umap.get(r[0]).encrypted_api_key) if umap.get(r[0]) else False,
            "calls": int(r[1]),
            "cost": round(float(r[2]), 6),
            "cost_cny": usd_to_cny(round(float(r[2]), 6)),
            "tokens": int(r[3]),
        }
        for r in top_rows
    ]

    # ---- Top 功能 ----
    op_rows = (
        db.query(
            Trace.operation,
            func.count(Trace.id),
            func.coalesce(func.sum(Trace.total_cost), 0),
            func.coalesce(func.avg(Trace.duration_ms), 0),
        )
        .group_by(Trace.operation)
        .order_by(func.count(Trace.id).desc())
        .limit(10)
        .all()
    )
    top_operations = [
        {
            "operation": r[0],
            "calls": int(r[1]),
            "cost": round(float(r[2]), 6),
            "cost_cny": usd_to_cny(round(float(r[2]), 6)),
            "avg_duration_ms": int(r[3]),
        }
        for r in op_rows
    ]

    # ---- 异常提示 ----
    alerts: List[Dict[str, str]] = []
    if calls == 0:
        alerts.append({
            "level": "warning",
            "message": "尚无任何 AI 调用记录。若确有使用，说明用量采集可能异常（检查 observability 落库）。",
        })
    elif _count(db, Trace, Trace.created_at >= since_1d) == 0:
        alerts.append({
            "level": "warning",
            "message": "最近 24 小时无 AI 调用记录，用量采集可能中断。",
        })
    if calls and usage["fail_rate"] >= 20:
        alerts.append({
            "level": "warning",
            "message": f"AI 调用失败率偏高（{usage['fail_rate']}%），建议查看系统健康页的失败明细。",
        })

    # 平台默认 Key 可用性
    try:
        from app.config import get_settings as _gs
        if not _gs().deepseek_api_key:
            alerts.append({"level": "error", "message": "平台默认 API Key 未配置，未自带 Key 的用户将无法使用 AI 功能。"})
    except Exception:  # noqa: BLE001
        pass

    quota = int(get_setting_value(db, "quota_daily_tokens", 0) or 0)
    if quota > 0:
        alerts.append({"level": "info", "message": f"已启用每日用量配额：每用户 {quota} tokens（仅平台默认 Key 用户）。"})

    return {
        "generated_at": now.isoformat(),
        "window_days": days,
        "users": users,
        "usage": usage,
        "content": content,
        "trend": trend,
        "top_users": top_users,
        "top_operations": top_operations,
        "alerts": alerts,
    }


# ============================================================
# 用量与成本：多维度
# ============================================================

USAGE_DIMENSIONS = ("user", "operation", "model", "day")


def get_usage(db: Session, dimension: str = "operation",
              start: Optional[datetime] = None, end: Optional[datetime] = None,
              limit: int = 50) -> Dict[str, Any]:
    """按 用户 / 功能 / 模型 / 日期 四个维度统计用量与成本"""
    if dimension not in USAGE_DIMENSIONS:
        raise ValueError(f"不支持的统计维度: {dimension}")

    if dimension == "model":
        # 模型维度来自 span 表（trace 不记录模型）
        q = db.query(
            Span.model,
            func.count(Span.id),
            func.coalesce(func.sum(Span.prompt_tokens), 0),
            func.coalesce(func.sum(Span.completion_tokens), 0),
            func.coalesce(func.sum(Span.cost), 0),
            func.coalesce(func.avg(Span.duration_ms), 0),
        )
        if start is not None:
            q = q.filter(Span.created_at >= start)
        if end is not None:
            q = q.filter(Span.created_at < end)
        rows = q.group_by(Span.model).order_by(func.count(Span.id).desc()).limit(limit).all()
        items = [
            {
                "key": r[0] or "(未知模型)",
                "calls": int(r[1]),
                "prompt_tokens": int(r[2]),
                "completion_tokens": int(r[3]),
                "tokens": int(r[2]) + int(r[3]),
                "cost": round(float(r[4] or 0), 6),
                "cost_cny": usd_to_cny(round(float(r[4] or 0), 6)),
                "avg_duration_ms": int(r[5] or 0),
            }
            for r in rows
        ]
        return {"dimension": dimension, "items": items}

    key_col = {
        "user": Trace.user_id,
        "operation": Trace.operation,
        "day": func.date(Trace.created_at),
    }[dimension]

    q = db.query(
        key_col.label("k"),
        func.count(Trace.id),
        func.coalesce(func.sum(Trace.total_tokens), 0),
        func.coalesce(func.sum(Trace.total_cost), 0),
        func.coalesce(func.avg(Trace.duration_ms), 0),
        func.count(Trace.id).filter(Trace.status != "success"),
    )
    if start is not None:
        q = q.filter(Trace.created_at >= start)
    if end is not None:
        q = q.filter(Trace.created_at < end)

    if dimension == "day":
        rows = q.group_by(key_col).order_by(key_col).all()
    else:
        rows = q.group_by(key_col).order_by(func.sum(Trace.total_cost).desc()).limit(limit).all()

    items = [
        {
            "key": str(r[0]) if r[0] is not None else "(未知)",
            "calls": int(r[1]),
            "tokens": int(r[2]),
            "cost": round(float(r[3]), 6),
            "cost_cny": usd_to_cny(round(float(r[3]), 6)),
            "avg_duration_ms": int(r[4] or 0),
            "failed": int(r[5] or 0),
        }
        for r in rows
    ]

    # 用户维度补名字/邮箱/BYOK
    if dimension == "user":
        uids = [i["key"] for i in items]
        umap = {u.id: u for u in db.query(User).filter(User.id.in_(uids)).all()} if uids else {}
        for i in items:
            u = umap.get(i["key"])
            i["name"] = u.name if u else "(已删除)"
            i["email"] = u.email if u else None
            i["byok"] = bool(u.encrypted_api_key) if u else False

    total = {
        "calls": sum(i["calls"] for i in items),
        "tokens": sum(i["tokens"] for i in items),
        "cost": round(sum(i["cost"] for i in items), 6),
        "cost_cny": usd_to_cny(round(sum(i["cost"] for i in items), 6)),
    }
    return {"dimension": dimension, "items": items, "total": total}


def list_traces(db: Session, page: int = 1, page_size: int = 20,
                user_id: Optional[str] = None, operation: Optional[str] = None,
                status: Optional[str] = None) -> Dict[str, Any]:
    """逐次调用明细"""
    q = db.query(Trace)
    if user_id:
        q = q.filter(Trace.user_id == user_id)
    if operation:
        q = q.filter(Trace.operation == operation)
    if status:
        q = q.filter(Trace.status == status)

    total = q.count()
    rows = (
        q.order_by(Trace.created_at.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
        .all()
    )
    uids = list({r.user_id for r in rows if r.user_id})
    umap = {u.id: u for u in db.query(User).filter(User.id.in_(uids)).all()} if uids else {}
    return {
        "total": total,
        "page": page,
        "page_size": page_size,
        "items": [
            {
                "id": r.id,
                "user_id": r.user_id,
                "user_name": umap.get(r.user_id).name if r.user_id in umap else None,
                "operation": r.operation,
                "status": r.status,
                "tokens": r.total_tokens,
                "cost": round(float(r.total_cost or 0), 6),
                "cost_cny": usd_to_cny(round(float(r.total_cost or 0), 6)),
                "duration_ms": r.duration_ms,
                "spans": r.span_count,
                "created_at": _fmt_dt(r.created_at),
            }
            for r in rows
        ],
    }


# ============================================================
# 用户管理（增强）
# ============================================================

def list_users_admin(db: Session, q: Optional[str] = None, role: Optional[str] = None,
                     is_active: Optional[bool] = None, byok: Optional[bool] = None,
                     page: int = 1, page_size: int = 20) -> Dict[str, Any]:
    query = db.query(User)
    if q:
        like = f"%{q.strip()}%"
        query = query.filter((User.name.ilike(like)) | (User.email.ilike(like)) | (User.id.ilike(like)))
    if role:
        query = query.filter(User.role == role)
    if is_active is not None:
        query = query.filter(User.is_active.is_(is_active))
    if byok is not None:
        query = query.filter(
            User.encrypted_api_key.isnot(None) if byok else User.encrypted_api_key.is_(None)
        )

    total = query.count()
    rows = (
        query.order_by(User.created_at.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
        .all()
    )

    # 附带每个用户的用量汇总
    ids = [u.id for u in rows]
    usage_map: Dict[str, Dict[str, Any]] = {}
    if ids:
        urows = (
            db.query(
                Trace.user_id,
                func.count(Trace.id),
                func.coalesce(func.sum(Trace.total_tokens), 0),
                func.coalesce(func.sum(Trace.total_cost), 0),
                func.max(Trace.created_at),
            )
            .filter(Trace.user_id.in_(ids))
            .group_by(Trace.user_id)
            .all()
        )
        for r in urows:
            usage_map[r[0]] = {
                "calls": int(r[1]),
                "tokens": int(r[2]),
                "cost": round(float(r[3]), 6),
                "cost_cny": usd_to_cny(round(float(r[3]), 6)),
                "last_active_at": _fmt_dt(r[4]),
            }

    items = []
    for u in rows:
        items.append({
            "id": u.id,
            "name": u.name,
            "email": u.email,
            "role": u.role,
            "is_active": bool(u.is_active),
            "byok": bool(u.encrypted_api_key),
            "allow_corpus": bool(u.allow_corpus),
            "llm_model": u.llm_model,
            "created_at": _fmt_dt(u.created_at),
            "usage": usage_map.get(
                u.id,
                {"calls": 0, "tokens": 0, "cost": 0.0, "cost_cny": 0.0, "last_active_at": None},
            ),
        })
    return {"total": total, "page": page, "page_size": page_size, "items": items}


def get_user_detail(db: Session, user_id: str) -> Optional[Dict[str, Any]]:
    u = db.query(User).filter(User.id == user_id).first()
    if not u:
        return None

    daily = get_daily_tokens_used(db, user_id)
    quota = int(get_setting_value(db, "quota_daily_tokens", 0) or 0)

    usage = {
        "calls": _count(db, Trace, Trace.user_id == user_id),
        "tokens": int(_sum(db, Trace.total_tokens, Trace.user_id == user_id)),
        "cost": round(_sum(db, Trace.total_cost, Trace.user_id == user_id), 6),
        "cost_cny": usd_to_cny(round(_sum(db, Trace.total_cost, Trace.user_id == user_id), 6)),
        "failed": _count(db, Trace, Trace.user_id == user_id, Trace.status != "success"),
        "today_tokens": daily,
        "quota_daily_tokens": quota,
    }

    # 近 14 天该用户用量
    since = (_utcnow() - timedelta(days=13)).replace(hour=0, minute=0, second=0, microsecond=0)
    day_col = func.date(Trace.created_at)
    rows = (
        db.query(day_col, func.count(Trace.id), func.coalesce(func.sum(Trace.total_cost), 0))
        .filter(Trace.user_id == user_id, Trace.created_at >= since)
        .group_by(day_col)
        .order_by(day_col)
        .all()
    )
    trend = [
        {
            "date": str(r[0]),
            "calls": int(r[1]),
            "cost": round(float(r[2]), 6),
            "cost_cny": usd_to_cny(round(float(r[2]), 6)),
        }
        for r in rows
    ]

    # operation 维度是全局的，这里单独取该用户的 Top 功能
    op_rows = (
        db.query(Trace.operation, func.count(Trace.id), func.coalesce(func.sum(Trace.total_cost), 0))
        .filter(Trace.user_id == user_id)
        .group_by(Trace.operation)
        .order_by(func.count(Trace.id).desc())
        .limit(10)
        .all()
    )

    return {
        "user": {
            "id": u.id,
            "name": u.name,
            "email": u.email,
            "role": u.role,
            "is_active": bool(u.is_active),
            "byok": bool(u.encrypted_api_key),
            "allow_corpus": bool(u.allow_corpus),
            "llm_provider": u.llm_provider,
            "llm_base_url": u.llm_base_url,
            "llm_model": u.llm_model,
            "created_at": _fmt_dt(u.created_at),
        },
        "usage": usage,
        "trend": trend,
        "top_operations": [
            {
                "operation": r[0],
                "calls": int(r[1]),
                "cost": round(float(r[2]), 6),
                "cost_cny": usd_to_cny(round(float(r[2]), 6)),
            }
            for r in op_rows
        ],
        "content": {
            "resumes": _count(db, Resume, Resume.user_id == user_id),
            "jds": _count(db, JobDescription, JobDescription.user_id == user_id),
            "applications": _count(db, Application, Application.user_id == user_id),
            "interview_sessions": _count(db, InterviewSession, InterviewSession.user_id == user_id),
            "corpus": _count(db, CorpusItem, CorpusItem.user_id == user_id),
            "posts": _count(db, CommunityPost, CommunityPost.user_id == user_id),
            "feedbacks": _count(db, Feedback, Feedback.user_id == user_id),
        },
    }


def export_users_csv(db: Session) -> str:
    """导出全部用户为 CSV 文本"""
    import csv
    import io as _io

    buf = _io.StringIO()
    w = csv.writer(buf)
    w.writerow(["id", "name", "email", "role", "is_active", "byok",
                "allow_corpus", "created_at", "calls", "tokens", "cost_usd"])

    usage_map: Dict[str, Any] = {}
    for r in db.query(
        Trace.user_id,
        func.count(Trace.id),
        func.coalesce(func.sum(Trace.total_tokens), 0),
        func.coalesce(func.sum(Trace.total_cost), 0),
    ).group_by(Trace.user_id).all():
        usage_map[r[0]] = (int(r[1]), int(r[2]), round(float(r[3]), 6))

    for u in db.query(User).order_by(User.created_at.asc()).all():
        calls, tokens, cost = usage_map.get(u.id, (0, 0, 0.0))
        w.writerow([
            u.id, u.name, u.email or "", u.role,
            "1" if u.is_active else "0",
            "1" if u.encrypted_api_key else "0",
            "1" if u.allow_corpus else "0",
            _fmt_dt(u.created_at) or "", calls, tokens, cost,
        ])
    return buf.getvalue()


# ============================================================
# 系统健康
# ============================================================

def get_health(db: Session, recent: int = 20) -> Dict[str, Any]:
    now = _utcnow()

    # 数据库
    try:
        db.query(func.count(User.id)).scalar()
        db_ok = True
    except Exception:  # noqa: BLE001
        db_ok = False
        logger.exception("健康检查: 数据库不可用")

    # 平台默认 Key 可用性（不实际调用外部 API，只看配置）
    default_key_set = False
    model_name = None
    try:
        from app.config import get_settings
        s = get_settings()
        default_key_set = bool(s.deepseek_api_key)
        model_name = s.llm_model
    except Exception:  # noqa: BLE001
        pass

    last_trace = db.query(func.max(Trace.created_at)).scalar()
    last_1h = _count(db, Trace, Trace.created_at >= now - timedelta(hours=1))
    last_24h = _count(db, Trace, Trace.created_at >= now - timedelta(days=1))

    failed_rows = (
        db.query(Trace)
        .filter(Trace.status != "success")
        .order_by(Trace.created_at.desc())
        .limit(recent)
        .all()
    )
    slow_rows = (
        db.query(Trace)
        .order_by(Trace.duration_ms.desc())
        .limit(recent)
        .all()
    )

    uids = list({r.user_id for r in (failed_rows + slow_rows) if r.user_id})
    umap = {u.id: u for u in db.query(User).filter(User.id.in_(uids)).all()} if uids else {}

    def _rows(rs):
        return [
            {
                "id": r.id,
                "user_id": r.user_id,
                "user_name": umap.get(r.user_id).name if r.user_id in umap else None,
                "operation": r.operation,
                "status": r.status,
                "tokens": r.total_tokens,
                "duration_ms": r.duration_ms,
                "created_at": _fmt_dt(r.created_at),
            }
            for r in rs
        ]

    status = "ok" if (db_ok and default_key_set) else ("degraded" if db_ok else "down")
    return {
        "checked_at": now.isoformat(),
        "status": status,
        "database": {"ok": db_ok},
        "default_key": {"configured": default_key_set, "model": model_name},
        "traces": {
            "total": _count(db, Trace),
            "last_1h": last_1h,
            "last_24h": last_24h,
            "last_at": _fmt_dt(last_trace),
        },
        "recent_failures": _rows(failed_rows),
        "slowest": _rows(slow_rows),
    }


# ============================================================
# 审计日志
# ============================================================

def list_audit_logs(db: Session, page: int = 1, page_size: int = 30,
                    action: Optional[str] = None,
                    operator_id: Optional[str] = None) -> Dict[str, Any]:
    q = db.query(AdminAuditLog)
    if action:
        q = q.filter(AdminAuditLog.action == action)
    if operator_id:
        q = q.filter(AdminAuditLog.operator_id == operator_id)

    total = q.count()
    rows = (
        q.order_by(AdminAuditLog.created_at.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
        .all()
    )
    actions = [
        r[0] for r in db.query(AdminAuditLog.action).distinct().order_by(AdminAuditLog.action).all()
    ]
    return {
        "total": total,
        "page": page,
        "page_size": page_size,
        "actions": actions,
        "items": [
            {
                "id": r.id,
                "operator_id": r.operator_id,
                "operator_name": r.operator_name,
                "action": r.action,
                "target_type": r.target_type,
                "target_id": r.target_id,
                "target_label": r.target_label,
                "detail": r.detail,
                "ip": r.ip,
                "created_at": _fmt_dt(r.created_at),
            }
            for r in rows
        ],
    }
