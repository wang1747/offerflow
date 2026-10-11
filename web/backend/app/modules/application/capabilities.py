"""投递领域能力（Agent 唯一允许的入口）。

设计依据：`docs/agent/v0-agent-capability-boundary.md` 第 4/5.2/9 节。

与 jd/resume/match 的 capabilities 同一套规矩：校验 ``user_id`` 归属、跨用户统一
``not_found``、不导入 services 的私有函数之外的东西（本文件**只**调用
``services`` 已有的公开函数与 ORM 模型，不重新实现业务算法）。

## 为什么这个写能力要用「期望状态 CAS」而不是版本号

``applications`` 表**没有 version 列**，只有 ``updated_at``。可选做法有三种：

1. 给表加一列 ``version``（乐观锁）——要动已有业务表，风险与收益不成比例；
2. 用 ``updated_at`` 当版本——时间戳精度在 SQLite/Postgres 上不一致，
   同一秒内的两次写入会互相放行，**这是个会静默失效的锁**；
3. **用期望的当前状态做 CAS**（本文件的实现）——投递状态本身就是一个小状态机，
   「我看到它是 ``saved``，我要把它改成 ``applied``」这种语义天然自洽，
   且不需要给业务表加列。

第 3 种就是设计文档 10 节 ``ActionIntent.expected_version`` 在本项目的落地形态：
``expected_version`` 存的就是期望的当前状态字符串。任何人在 Agent 提议之后
先改过这条投递，CAS 就会失败并返回 ``conflict``——**这正是防止
「基于过期上下文写入」的那道闸门**（22.1 节：上下文冲突和过期时禁止执行写操作）。

## 幂等在哪里

**不在本文件**。写工具的幂等键由 Agent 的 ``agent_action_intents`` 表承担
（见 ``policies.py``）：意图表按 ``idempotency_key`` 唯一约束，重复确认只会
命中已有记录而不会二次执行。这样业务表不必为 Agent 的机制加列。
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

from app.models.application import Application, ApplicationEvent
from app.modules.application.services import APPLICATION_STATUSES

logger = logging.getLogger(__name__)

#: 模型给出的变更理由进业务表（``application_events.description``）的上限。
#: 它是**模型自由文本**，所以既限长又禁多行——否则一次提示词注入就能把
#: 一段 JD 正文写进用户的投递事件里。
MAX_REASON_CHARS = 200

#: 允许 Agent 写入的状态。与 ``application_status`` 的完整枚举一致，
#: **但 ``accepted`` / ``rejected`` 这类"用户对自己处境下的最终判断"不放开**：
#: 让 Agent 提议把一条投递标成"已拒绝"没有产品意义，反而扩大了误写面。
AGENT_WRITABLE_STATUSES = (
    "saved", "applied", "online_test", "first_interview",
    "second_interview", "hr_round", "offered",
)


def _iso(value: Any) -> Optional[str]:
    if isinstance(value, datetime):
        return value.isoformat()
    return None


def _clip_reason(reason: Optional[str]) -> str:
    text = " ".join((reason or "").split())
    return text[:MAX_REASON_CHARS]


def _app_ref(app: Application) -> Dict[str, Any]:
    return {
        "type": "application",
        "id": app.id,
        "version": None,
        "updated_at": _iso(app.updated_at),
    }


def _app_view(app: Application) -> Dict[str, Any]:
    return {
        "id": app.id,
        "company": app.company or "",
        "position": app.position or "",
        "status": app.status,
        "next_action": app.next_action,
        "updated_at": _iso(app.updated_at),
    }


# ---------------------------------------------------------------------------
# 能力 1（只读）：application.get
# ---------------------------------------------------------------------------
def get_application_capability(
    db: Session, user_id: str, application_id: str,
) -> Dict[str, Any]:
    """读取一条投递记录的状态与引用。不返回备注正文（``notes`` 可能是长文本）。"""
    app = db.query(Application).filter(
        Application.id == application_id,
        Application.user_id == user_id,
    ).first()
    if not app:
        return {"status": "not_found", "message": "投递记录不存在"}

    return {
        "status": "succeeded",
        "application": _app_view(app),
        "available_statuses": list(APPLICATION_STATUSES),
        "agent_writable_statuses": list(AGENT_WRITABLE_STATUSES),
        "object_refs": [_app_ref(app)],
        # 无 version 列：用「状态 + 更新时间」表达对象版本，诚实标注
        "object_version": f"application:{app.id}:{app.status}",
    }


# ---------------------------------------------------------------------------
# 能力 2（写入）：application.update_status
# ---------------------------------------------------------------------------
def update_status_capability(
    db: Session,
    user_id: str,
    application_id: str,
    expected_status: str,
    new_status: str,
    reason: str = "",
) -> Dict[str, Any]:
    """把投递状态从 ``expected_status`` 改成 ``new_status``（CAS + 事务内写事件）。

    返回的 ``status`` 直接落进 ``ToolResult.status``，取值来自设计文档 6.2 的
    统一枚举：``succeeded`` / ``not_found`` / ``conflict`` / ``failed``。

    三条防线：
    - ``not_found``：投递不存在**或不属于该用户**（信息泄露防护，不区分两者）；
    - ``conflict``：当前状态 ≠ ``expected_status``——用户在 Agent 提议之后
      已经手动改过这条投递，**必须停止写入并要求重新读取**（22.1 节）；
    - ``failed``：目标状态非法或不在 Agent 可写集合内。
    """
    app = db.query(Application).filter(
        Application.id == application_id,
        Application.user_id == user_id,
    ).first()
    if not app:
        return {"status": "not_found", "message": "投递记录不存在"}

    if new_status not in APPLICATION_STATUSES:
        return {
            "status": "failed",
            "reason": "invalid_status",
            "message": f"目标状态非法：{new_status}",
        }
    if new_status not in AGENT_WRITABLE_STATUSES:
        return {
            "status": "failed",
            "reason": "status_not_agent_writable",
            "message": f"该状态不允许由 Agent 写入：{new_status}",
        }

    # ---- CAS：这是整个写路径最关键的一行 --------------------------------
    if app.status != expected_status:
        return {
            "status": "conflict",
            "reason": "status_conflict",
            "message": (
                f"投递状态已变化（期望 {expected_status}，实际 {app.status}），"
                "请重新读取后再确认"
            ),
            "current_status": app.status,
            "object_refs": [_app_ref(app)],
        }

    previous_status = app.status
    try:
        # 状态与事件**在同一事务**里落库：只改状态不写事件会让投递时间线丢一段，
        # 而时间线正是这个模块的产品价值。
        app.status = new_status
        event = ApplicationEvent(
            application_id=app.id,
            event_type="status_change",
            from_status=previous_status,
            to_status=new_status,
            description=(
                f"状态变更（Agent 提议、用户确认）: {previous_status} → {new_status}"
                + (f"；理由：{_clip_reason(reason)}" if _clip_reason(reason) else "")
            ),
            event_date=datetime.now(),
        )
        db.add(event)
        db.commit()
        db.refresh(app)
        db.refresh(event)
    except Exception:  # noqa: BLE001
        db.rollback()
        # 只回结构化错误，异常栈不外泄（与工具网关同一策略）
        logger.warning("投递状态写入失败：application_id=%s", application_id, exc_info=True)
        return {
            "status": "failed",
            "reason": "write_failed",
            "message": "投递状态写入失败，请稍后重试",
        }

    return {
        "status": "succeeded",
        "application": _app_view(app),
        "previous_status": previous_status,
        "event_id": event.id,
        "object_refs": [_app_ref(app)],
        # 写成功后对象版本随之推进：``application:{id}:{新状态}``。
        # 这是 **capability 返回的业务事实**，Agent 不得自行构造（10 节）。
        "object_version": f"application:{app.id}:{app.status}",
    }
