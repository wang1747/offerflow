"""薪资基准数据存取层（桌面版）：把基准数据从「代码写死」升级为「本机版本化」。

Web 版放在 DB 表 `salary_benchmarks`（全局共享、is_active 标记生效版本、旧版可回滚）；
桌面版对等存在本机 `local_store` 的单例记录里 —— entity=`salary_benchmark`、id=`active`。

## 为什么用「单例一条」而不是版本表

桌面是单机单用户，没有「多账号共享一份基准数据」的诉求，也没有回滚审计的合规压力。
保留**版本号自增**是为了让界面能说清「你这次算的是第几版行情」，而不是为了回滚。

## 已知语义差异（有意为之，不是缺陷）

基准数据**不打服务器**，两端各管一份。原因有两条，都不可绕过：
1. 服务端的 `benchmark/info` 只回**元信息**（version/data_year/source），不回数据表本体
   —— 桌面端根本没有拿到权威数据表的通道，想同步也无从下手；
2. 反过来把本机导入的数据上行，改的是**所有账号**的市场价计算
   （服务端 `salary_benchmark_store` 的读写都不带 user 过滤），
   代价与知情成本远超「多端一致」的收益。

需要跨端一致时，把服务端的基准数据导出后在桌面端再导入一次即可。

## 未登录时的行为（诚实优先）

- **读**：未登录 / 无记录 → 回退内置基线（`version=0, source="内置基线"`）。
  薪资参考在没登录时也照样能算，不摆脸色。
- **写**：未登录 → 抛 `ValueError`。绝不静默成功 —— 用户会以为导入生效了，
  实际什么都没落盘，下次算出来的还是旧价。
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Dict, Optional

from .. import local_store
from . import salary_benchmark as benchmark

logger = logging.getLogger(__name__)

ENTITY = "salary_benchmark"
SINGLETON_ID = "active"

DEFAULT_SOURCE = "2026 届应届生起薪市场调研（智联猎头/福睿思特/麦可思/新东方）"
DEFAULT_DATA_YEAR = "2026"

_NOT_LOGGED_IN = "未登录，无法保存薪资基准数据（请先登录后重试）"


def _read() -> Optional[Dict[str, Any]]:
    """读本机生效记录；未登录 / 无记录 / 解析失败统一返回 None。"""
    try:
        rec = local_store.get_item(ENTITY, SINGLETON_ID)
    except Exception as exc:  # noqa: BLE001
        logger.warning("读取本机薪资基准失败：%s: %s", type(exc).__name__, exc)
        return None
    return rec if isinstance(rec, dict) else None


def active_data() -> Dict[str, Any]:
    """当前生效基准数据（含元信息）。

    结构对齐 Web 版 `benchmark_store.active_data`：
    `{degree_base, direction_coef, city_tiers, version, data_year, source, note, updated_at}`。
    无记录时回退内置默认数据，`version=0` / `source="内置基线"` —— 界面据此显示
    「还没有导入过权威数据」，而不是假装这是一版正式数据。
    """
    rec = _read()
    if rec:
        return {
            "degree_base": rec.get("degree_base") or {},
            "direction_coef": rec.get("direction_coef") or {},
            "city_tiers": rec.get("city_tiers") or {},
            "version": int(rec.get("version") or 0),
            "data_year": rec.get("data_year") or DEFAULT_DATA_YEAR,
            "source": rec.get("source") or "",
            "note": rec.get("note") or "",
            "updated_at": rec.get("updated_at"),
        }
    return {
        **benchmark.default_data(),
        "version": 0,
        "data_year": DEFAULT_DATA_YEAR,
        "source": "内置基线",
        "note": "",
        "updated_at": None,
    }


def refresh(new_data: Dict[str, Any], data_year: str = "", source: str = "",
            note: str = "", report: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """写入新一版基准数据（覆盖生效数据），返回写入后的 `active_data()`。

    `new_data` 经 `benchmark.sanitize_data` 校验；不合法直接抛 `ValueError`，
    **不写任何东西** —— 脏数据一旦落盘，之后每次算价都是错的，且没有任何提示。

    `report`（可选出参，透传给 `sanitize_data`）会把「哪些字段被静默兜底 / 丢弃」
    记下来，供调用方在界面上如实告知（见 `sanitize_data` 的 docstring）。

    版本号取 `当前 version + 1`，保证单调递增（界面上的「第 N 版」不会突然回退）。
    """
    cleaned = benchmark.sanitize_data(new_data, report=report)
    if not cleaned:
        raise ValueError("基准数据不合法（需要 degree_base / direction_coef / city_tiers 三张表），已拒绝写入")

    cur = _read() or {}
    try:
        version = int(cur.get("version") or 0) + 1
    except (TypeError, ValueError):
        version = 1

    payload: Dict[str, Any] = {
        "id": SINGLETON_ID,
        "version": version,
        "data_year": data_year or DEFAULT_DATA_YEAR,
        "source": source or DEFAULT_SOURCE,
        "note": note or "",
        "updated_at": datetime.now().isoformat(timespec="seconds"),
        **cleaned,
    }
    # state="synced" ⇒ 不进 outbox：基准数据不打服务器（见模块 docstring）。
    # 未登录时 restore 返回 False —— 必须当场报错，不能假装写成功。
    if not local_store.restore(ENTITY, SINGLETON_ID, payload, state="synced"):
        reason = local_store.last_error()
        raise ValueError(f"{_NOT_LOGGED_IN}：{reason}" if reason else _NOT_LOGGED_IN)

    logger.info("薪资基准数据已更新至 v%s（%s，%s）", version, payload["data_year"], payload["source"])
    return active_data()


def info() -> Dict[str, Any]:
    """当前生效基准数据的版本信息（供界面展示数据新鲜度）。"""
    active = active_data()
    return {
        "version": active.get("version") or 0,
        "data_year": active.get("data_year") or DEFAULT_DATA_YEAR,
        "source": active.get("source") or "",
        "note": active.get("note") or "",
        "updated_at": active.get("updated_at"),
    }


__all__ = ["ENTITY", "SINGLETON_ID", "DEFAULT_SOURCE", "DEFAULT_DATA_YEAR",
           "active_data", "refresh", "info"]
