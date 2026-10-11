"""简历版本文档规则：一份简历 = 一个 document，每次变更 = 追加一条不可变版本。

## 心智模型

- 一条 ``Resume`` 记录 = **一个不可变版本**；
- 同一份简历的多个版本通过 ``parsed_json.document_id`` 关联；
- **新上传的简历 = 一个全新的 document**（``document_id`` = 自己的 id，``version`` = 1），
  不再按用户全局递增 —— 两份互不相关的简历都从 v1 开始；
- **只追加，不修改**：优化、逐段重写、候选保存、回滚一律走 `create_next_version()`，
  旧版本的 ``raw_text`` / ``parsed_json`` 永远不动。

## 兼容历史数据

上线前写入的记录没有 ``document_id``，靠原有的 ``parsed_json.parent_id`` 链回溯到根记录，
因此新旧数据可以共存、无需迁移。

## v0 取舍

**暂不做 (document_id, version) 数据库唯一约束**（``document_id`` 当前在 JSON 列里，
无法直接建联合唯一键）。单 worker 下用进程锁串行化“读最大值 + 插入”，避免并发
生成相同版本号；未来改多 worker 时必须补数据库约束、独立列或分布式锁。
"""

import logging
import threading
import uuid
from typing import Dict, List, Optional

from sqlalchemy.orm import Session

from app.models.resume import Resume

logger = logging.getLogger(__name__)

# 当前后端是单 worker、DB 访问会在线程池并发执行；计算 max(version)+1 与插入之间
# 必须串行，否则同一文档的两个请求可能同时拿到 v2。多 worker 部署时仍应迁移到
# 数据库唯一约束或分布式锁。
_VERSION_LOCK = threading.Lock()


def _parsed_json(resume: Resume) -> Dict:
    value = getattr(resume, "parsed_json", None)
    return value if isinstance(value, dict) else {}


def by_id_map(db: Session, user_id: str) -> Dict[str, Resume]:
    """当前用户的全部简历 {id: Resume}，供 parent_id 链回溯使用（旧数据兼容）。"""
    rows = db.query(Resume).filter(Resume.user_id == user_id).all()
    return {row.id: row for row in rows}


def document_id(resume: Resume, by_id: Optional[Dict[str, Resume]] = None) -> str:
    """返回版本所属的稳定文档 ID。

    优先取 ``parsed_json.document_id``；老数据没有时沿 ``parent_id`` 链回溯到根记录。
    没有 by_id 时退化为"自己就是一份文档"。
    """
    explicit = _parsed_json(resume).get("document_id")
    if explicit:
        return str(explicit)

    if not by_id:
        return resume.id

    seen = set()
    current_id = resume.id
    while current_id and current_id not in seen:
        seen.add(current_id)
        item = by_id.get(current_id)
        if not item:
            break
        parent = _parsed_json(item).get("parent_id")
        if not parent:
            return current_id
        current_id = str(parent)
    return resume.id


def get_document_versions(
    db: Session,
    user_id: str,
    resume: Resume,
    by_id: Optional[Dict[str, Resume]] = None,
) -> List[Resume]:
    """同一份简历的全部不可变版本，按版本号倒序。"""
    mapping = by_id if by_id is not None else by_id_map(db, user_id)
    target = document_id(resume, mapping)
    versions = [row for row in mapping.values() if document_id(row, mapping) == target]
    versions.sort(key=lambda row: row.version or 0, reverse=True)
    return versions


def same_document(db: Session, user_id: str, left: Resume, right: Resume) -> bool:
    """两个版本是否属于同一份简历（跨文档回滚必须据此拒绝）。"""
    mapping = by_id_map(db, user_id)
    return document_id(left, mapping) == document_id(right, mapping)


def create_next_version(
    db: Session,
    user_id: str,
    source: Resume,
    raw_text: str,
    parsed_json: Optional[Dict] = None,
    source_file: Optional[str] = None,
    rolled_back_from: Optional[int] = None,
) -> Resume:
    """以 ``source`` 所属的文档为准，追加一条新版本；旧版本一个字节都不改。

    - 版本号 = 同文档现有最大版本号 + 1；
    - 继承 ``document_id``，并把 ``parent_id`` 指向 ``source``（保持回溯链完整）；
    - ``rolled_back_from`` 仅在回滚时传入，记录"这一版是从第几版复制回来的"。
    """
    with _VERSION_LOCK:
        mapping = by_id_map(db, user_id)
        target_document = document_id(source, mapping)
        versions = [
            row for row in mapping.values()
            if document_id(row, mapping) == target_document
        ]
        next_version = max((row.version or 0 for row in versions), default=0) + 1

        payload = dict(parsed_json or {})
        # source_photo 属于文档级元数据，不是某一版的业务内容。优化、生成、
        # 回滚都必须默认继承；否则只要某个调用方漏抄一次，后续所有导出都会丢证件照。
        if "source_photo" not in payload:
            source_photo = _parsed_json(source).get("source_photo")
            if source_photo:
                payload["source_photo"] = source_photo
        payload["document_id"] = target_document
        payload["parent_id"] = source.id
        if rolled_back_from is not None:
            payload["rolled_back_from"] = rolled_back_from

        version = Resume(
            id=str(uuid.uuid4()),
            user_id=user_id,
            version=next_version,
            raw_text=raw_text,
            parsed_json=payload,
            source_file=source_file if source_file is not None else source.source_file,
        )
        db.add(version)
        db.commit()
        db.refresh(version)
        logger.info(
            "简历追加新版本: document=%s version=%s parent=%s",
            target_document,
            next_version,
            source.id,
        )
        return version


def update_payload(db: Session, resume: Resume, extra: Dict) -> Resume:
    """给**刚创建的**版本补充 payload（JSON 列不追踪原地修改，必须整体赋新对象）。

    只用于创建后的同一次写入收尾：这样调用方可以拿到新版本号和 id 之后再拼
    ``generation`` 这类需要回填自身信息的字段，避免为了拼 payload 而多写一版。
    """
    merged = dict(_parsed_json(resume))
    merged.update(extra or {})
    resume.parsed_json = merged
    db.add(resume)
    db.commit()
    db.refresh(resume)
    return resume
