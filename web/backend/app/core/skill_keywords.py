"""技能关键词词频库：加载 resume_skill_keywords 表的真实 JD 词频，增强技能识别与推荐。

数据来源：GitHub ResumeSample 各岗位 md 里的「参考技能关键字」词频表（从数百份真实 JD 统计）。
用途：① ATS 关键词识别（技术岗 JD 匹配更准）② 推荐技能（按岗位方向取 top N）。
"""

import sqlite3
import threading
from typing import Dict, List, Optional, Set, Tuple

from app.config import DB_PATH, get_settings

# 进程级缓存：direction -> [(keyword, freq)]（按词频降序）
_freq_data: Optional[Dict[str, List[Tuple[str, int]]]] = None
_keyword_set: Optional[Set[str]] = None
_lock = threading.Lock()


def _db_path() -> str:
    url = get_settings().database_url
    if url.startswith("sqlite:///"):
        return url[len("sqlite:///"):]
    return str(DB_PATH)


def load_frequency() -> Dict[str, List[Tuple[str, int]]]:
    """懒加载词频表，进程级缓存。direction -> [(keyword, freq)]（词频降序）。"""
    global _freq_data, _keyword_set
    if _freq_data is not None:
        return _freq_data
    with _lock:
        if _freq_data is not None:
            return _freq_data
        data: Dict[str, List[Tuple[str, int]]] = {}
        kws: Set[str] = set()
        try:
            conn = sqlite3.connect(_db_path())
            rows = conn.execute(
                "SELECT direction, keyword, frequency FROM resume_skill_keywords ORDER BY frequency DESC"
            ).fetchall()
            conn.close()
            for direction, keyword, freq in rows:
                data.setdefault(direction, []).append((keyword, int(freq)))
                kws.add(keyword.lower())
        except Exception:
            # 表不存在或数据库未就绪时静默降级为空，不影响主流程
            pass
        _freq_data = data
        _keyword_set = kws
        return data


def keyword_set() -> Set[str]:
    """所有真实 JD 技术关键词（小写），用于技能识别。"""
    if _keyword_set is None:
        load_frequency()
    return _keyword_set or set()


def top_keywords(direction: str, n: int = 12) -> List[str]:
    """某岗位方向的 top N 技能关键词。"""
    data = load_frequency()
    for d, kws in data.items():
        if d == direction:
            return [k for k, _ in kws[:n]]
    return []


def directions() -> List[str]:
    """所有已入库的岗位方向标签。"""
    return list(load_frequency().keys())
