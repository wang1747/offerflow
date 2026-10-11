"""薪资基准数据存取层：从 DB 读生效数据，提供 seed / 刷新。

把基准数据从「代码写死」升级为「落库版本化」：
- 首次启动 seed 内置基线（v1）；
- 定期 / 手动刷新写入新版本，旧版本保留 is_active=False 可回滚；
- 读取时返回当前 is_active 版本，读不到回退内置默认数据。
"""

import logging
from datetime import datetime
from typing import Optional

from sqlalchemy.orm import Session

from app.core import salary_benchmark as benchmark
from app.models.salary_benchmark import SalaryBenchmark

logger = logging.getLogger(__name__)

DEFAULT_SOURCE = "2026 届应届生起薪市场调研（智联猎头/福睿思特/麦可思/新东方）"
DEFAULT_DATA_YEAR = "2026"


def seed_default(db: Session) -> Optional[SalaryBenchmark]:
    """若无任何基准数据，写入内置默认数据作为 v1。已有数据则不动作。"""
    existing = db.query(SalaryBenchmark).first()
    if existing:
        return None
    data = benchmark.default_data()
    row = SalaryBenchmark(
        version=1,
        degree_base=data["degree_base"],
        direction_coef=data["direction_coef"],
        city_tiers=data["city_tiers"],
        data_year=DEFAULT_DATA_YEAR,
        source=DEFAULT_SOURCE,
        note="内置基线数据",
        is_active=True,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    logger.info("已 seed 薪资基准数据 v1")
    return row


def get_active_row(db: Session) -> Optional[SalaryBenchmark]:
    """获取当前生效的基准数据行。"""
    return (
        db.query(SalaryBenchmark)
        .filter(SalaryBenchmark.is_active == True)  # noqa: E712
        .order_by(SalaryBenchmark.version.desc())
        .first()
    )


def active_data(db: Session) -> dict:
    """返回当前生效基准数据（含元信息），无则回退内置默认数据。"""
    row = get_active_row(db)
    if row:
        return {
            "degree_base": row.degree_base or {},
            "direction_coef": row.direction_coef or {},
            "city_tiers": row.city_tiers or {},
            "version": row.version,
            "data_year": row.data_year or DEFAULT_DATA_YEAR,
            "source": row.source or "",
            "note": row.note or "",
            "updated_at": row.updated_at.isoformat() if row.updated_at else None,
        }
    return {
        **benchmark.default_data(),
        "version": 0,
        "data_year": DEFAULT_DATA_YEAR,
        "source": "内置基线",
        "note": "",
        "updated_at": None,
    }


def refresh(
    db: Session,
    new_data: dict,
    data_year: str,
    source: str,
    note: str,
) -> SalaryBenchmark:
    """写入新版本基准数据（旧版本置 inactive，保留可回滚）。"""
    cleaned = benchmark.sanitize_data(new_data)
    if not cleaned:
        raise ValueError("基准数据不合法，已拒绝写入")

    db.query(SalaryBenchmark).filter(
        SalaryBenchmark.is_active == True  # noqa: E712
    ).update({"is_active": False})

    latest = db.query(SalaryBenchmark).order_by(SalaryBenchmark.version.desc()).first()
    new_version = (latest.version + 1) if latest else 1

    row = SalaryBenchmark(
        version=new_version,
        degree_base=cleaned["degree_base"],
        direction_coef=cleaned["direction_coef"],
        city_tiers=cleaned["city_tiers"],
        data_year=data_year,
        source=source,
        note=note,
        is_active=True,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    logger.info("薪资基准数据已刷新至 v%s（%s）", new_version, data_year)
    return row
