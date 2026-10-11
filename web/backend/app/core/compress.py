"""headroom token 压缩接入 OfferFlow（带优雅降级）。

接入点：modules/corpus 检索返回 chunks 后、agents 拼 prompt 前，调用 compress_chunks()
把超长 chunk / 工具输出 / 长上下文压缩，再喂 DeepSeek，省 token、提速。

- HEADROOM_ENABLED=true 且库可用时走真实压缩（headroom 装好后替换 compress 实现）。
- 否则退化为按 max_chars 截断（保证不超模型上下文）。
"""
from __future__ import annotations

import logging
import os
from typing import Any, Dict, List

log = logging.getLogger("offerflow.compress")

HEADROOM_ENABLED = os.getenv("HEADROOM_ENABLED", "false").lower() == "true"
# 内存护栏：小机可用内存 < 此阈值时强制禁用真实 headroom，避免重压缩拖垮整机
_MEM_GUARD_MB = 1200.0
_hr = None  # None=未初始化; False=不可用


def _free_mem_mb() -> float:
    """读取 /proc/meminfo 的 MemAvailable，单位 MB。读取失败返回大值（不拦截）。"""
    try:
        with open("/proc/meminfo") as f:
            info = dict(line.split(":", 1) for line in f if ":" in line)
        val = info.get("MemAvailable", info.get("MemFree", "0")).strip().split()[0]
        return int(val) / 1024.0
    except Exception:
        return 9999.0


def _get_hr():
    global _hr
    if _hr is not None:
        return _hr
    if not HEADROOM_ENABLED:
        _hr = False
        return _hr
    if _free_mem_mb() < _MEM_GUARD_MB:
        log.warning("可用内存 %.0fMB < %sMB，禁用 headroom 真实压缩（防拖垮小机）", _free_mem_mb(), _MEM_GUARD_MB)
        _hr = False
        return _hr
    try:
        import headroom  # 真实压缩库

        _hr = headroom
        log.info("headroom 压缩已启用")
    except Exception as e:
        log.warning("headroom 不可用，降级为截断: %s", e)
        _hr = False
    return _hr


def compress_chunks(chunks: List[str], max_chars: int = 4000) -> List[str]:
    """压缩一组文本块。无库时按 max_chars 截断。"""
    hr = _get_hr()
    if hr and hasattr(hr, "compress"):
        try:
            return [hr.compress(c, max_chars=max_chars) for c in chunks]
        except Exception as e:
            log.warning("headroom 压缩失败，降级截断: %s", e)
    return [c[:max_chars] for c in chunks]


def compress_text(text: str, max_chars: int = 8000) -> str:
    """压缩单段长文本。"""
    return compress_chunks([text], max_chars)[0]


def compress_status() -> Dict[str, Any]:
    """返回 headroom 压缩层当前状态，供 /integrations/status 聚合。"""
    try:
        import headroom  # noqa: F401
        lib_available = True
    except Exception:
        lib_available = False
    using_real = bool(HEADROOM_ENABLED) and lib_available and _free_mem_mb() >= _MEM_GUARD_MB and _hr is not False
    return {
        "enabled": HEADROOM_ENABLED,
        "library_available": lib_available,
        "active": using_real,
        "backend": "headroom" if using_real else "truncation_fallback",
        "free_mem_mb": round(_free_mem_mb(), 1),
        "guard_mb": _MEM_GUARD_MB,
    }
