"""双向同步（P4）：每 30 分钟把本地待上行改动推给服务器，并把服务器最新拉回本地。

## 周期为什么硬编码

计划已定：**禁实时、不进设置、用户不可改**。桌面版的数据以本地为主存，
服务器是「同一份数据的另一个副本」；30 分钟一轮既能让多端最终一致，
又不会让用户在输入时被网络抖动打断。

## 一轮同步做什么

1. **上行**：按入队顺序处理 `local_store` 的 outbox。
   - 网络不可达 → 立即停手（后面必然也失败），保留队列下次再来；
   - 服务端业务错误（4xx）→ 记失败并继续下一条（不能一条坏数据卡死整个队列，
     也不会丢 —— 记录留在本地，用户看得到）；
   - `create` 成功后**再补一次 PUT**：create 端点不收的字段（status/notes/…）
     离线期间改了也得上得去，否则同步完被下行覆盖、改动静默消失。
2. **下行**：拉各实体全量并落本地（复用 `api_client` 的读函数，
   它们自带「归一 + 写镜像 + 写本地主存」）。下行**不会覆盖**本地未上行的改动
   （`local_store.replace_synced` 的保护规则）。

## 幂等

上行新建时会带上客户端生成的 id，服务端保证「同用户同 id 只建一条」，
所以哪怕这一轮推成功、下一轮又重推（例如回填前进程被杀），也不会产生重复数据。

## 已 P4 化的实体（离线可写）

| entity         | 上行 | 下行 | 备注 |
|----------------|------|------|------|
| applications   | ✅ | ✅ | 主记录 + create 后补 PUT（见下） |
| resumes        | ✅ | ✅ | 只有 create / delete（服务端无更新接口） |
| jds            | ✅ | ✅ | `/jd/parse` 带 id 幂等**且不二次调 LLM**；PUT 只改公司/职位 |
| articles（面经）| ✅ | ✅ | 服务端去重命中会返回**已有记录 id** → `resolve_op(server_id=…)` 会迁移本地主键 |

新增可写实体时要做三件事：`api_client` 的读写改本地优先、这里补 `_spec_for` 分支、
补 `_DOWNLOADERS`（否则新数据只上行、不下行）。

## 有意**不**同步的三类数据（不是遗漏）

面经（`articles`）已经在表里；下面三类**刻意**留在本机，各自的理由不同：

| 数据 | 存哪 | 为什么不上行 / 不下行 |
|------|------|----------------------|
| 匹配结果 `matches` | `local_store` | 服务端 `POST /match/` 会用它自己的 Chroma 重算，分数口径与本地不同。上行后下行会把本地分数覆盖掉，用户看到「同一个岗位的分数自己变了」。匹配是**随时可重算的派生数据**，不同步不损失能力。`api_client.list_matches()` 仍会合并云端 rankings（网页版算过的记录要能看见），只是**本机优先**。 |
| 面试会话 / 谈判会话 | 进程内存（`services/interview.py`、`services/salary_negotiation.py` 的模块级表） | 两边的服务端都只有 start / answer / summary 三个端点，**没有「列出历史会话」**，用户拿不到旧 `session_id`、本来也续接不了 ⇒ 放内存不损失可达能力。而且走 `local_store` 落库在未登录时直接失效，会把「不登录也能练面试」这条更重要的性质牺牲掉。代价：关掉应用后会话不保留。 |
| 薪资基准数据 | `local_store`（单例一条） | 服务端 `benchmark/info` 只回**元信息**、不回数据表，桌面端根本没有拿到权威数据表的通道；反过来自动上行会改动**所有账号**的算价（服务端那张表是全局的）。详见 `core/salary_benchmark_store` 的模块 docstring。 |
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Callable, Dict, Optional

from .. import api_client, local_store

logger = logging.getLogger(__name__)

# 同步周期（秒）。计划已定：硬编码 30 分钟，不进设置、用户不可改。
SYNC_INTERVAL = 30 * 60

# 下行：直接复用 api_client 的读函数（它们自带归一 + 镜像 + 落本地主存）
_DOWNLOADERS: Dict[str, Callable[[], Any]] = {
    "applications": api_client.list_applications,
    "resumes": api_client.list_resumes,
    "jds": api_client.list_jds,
    "articles": api_client.list_articles,
}


def _spec_for(entity: str, op: str, item_id: str,
              item: Optional[Dict[str, Any]]):
    """把一条本地改动翻译成 `(method, path, body)`；该实体还不支持离线写则返回 None。

    注意 `item` 对 delete 是 None（本地删除不保存原内容），
    主键要从 outbox 的 id 列取，不能从 payload 取。
    """
    iid = str(item_id or "")
    it = item or {}
    if entity == "applications":
        if op == "create":
            return ("POST", "/api/v1/applications/",
                    {"company": it.get("company", ""), "position": it.get("position", ""),
                     "jd_id": it.get("jd_id"), "resume_id": it.get("resume_id"), "id": iid})
        if op == "update":
            # 服务端 UpdateApplicationRequest 各字段都是 Optional：None 表示「不改」
            return ("PUT", f"/api/v1/applications/{iid}",
                    {"status": it.get("status"), "applied_date": it.get("applied_date"),
                     "next_action": it.get("next_action"),
                     "next_action_date": it.get("next_action_date"), "notes": it.get("notes")})
        if op == "delete":
            return ("DELETE", f"/api/v1/applications/{iid}", None)
    if entity == "resumes":
        if op == "create":
            return ("POST", "/api/v1/resume/upload",
                    {"raw_text": it.get("raw_text", ""),
                     "source_file": it.get("source_file") or None, "id": iid})
        if op == "delete":
            return ("DELETE", f"/api/v1/resume/{iid}", None)
    if entity == "jds":
        # 服务端 `/jd/parse` 顺带做 LLM 解析；带上 id 后重复上行不会二次解析、也不会建重
        if op == "create":
            return ("POST", "/api/v1/jd/parse", {"raw_text": it.get("raw_text", ""), "id": iid})
        if op == "update":
            return ("PUT", f"/api/v1/jd/{iid}",
                    {"company": it.get("company"), "position": it.get("position")})
        if op == "delete":
            return ("DELETE", f"/api/v1/jd/{iid}", None)
    if entity == "articles":
        # 服务端做去重 + 抽题打标；重复上行命中同 id 时直接返回，不再调 LLM
        if op == "create":
            return ("POST", "/api/v1/interview/articles",
                    {"company": it.get("company", ""), "position": it.get("position") or "",
                     "raw_content": it.get("raw_content", ""),
                     "source": it.get("source") or "manual", "id": iid})
        if op == "delete":
            return ("DELETE", f"/api/v1/interview/articles/{iid}", None)
    return None


# create 端点只认「新建那一刻就存在」的少数字段（company/position/jd_id/resume_id/id，
# 见后端 CreateApplicationRequest）。桌面版离线期间补填的其余字段（状态/备注/下一步…）
# 无法随 create 提交 —— 不处理的话，「离线新建 → 再编辑」的改动会在同步后被下行覆盖，
# 静静消失（实测坐实的真 bug）。
#
# 处理：create 成功后再补一次 PUT（服务端此时已有该记录）。失败时把改动**重新入队**，
# 下一轮以 update 的形式再补 —— 绝不静默丢。
_APP_LATE_FIELDS = ("status", "applied_date", "next_action", "next_action_date", "notes")


def _late_fields_for(entity: str, item: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """create 之后需要用 PUT 补齐的字段；与服务端默认值相同的就不白跑一趟。"""
    if entity != "applications":
        return None
    it = item or {}
    body: Dict[str, Any] = {}
    for key in _APP_LATE_FIELDS:
        val = it.get(key)
        if val in (None, "", "saved"):     # "saved" 是后端 create 的默认状态
            continue
        body[key] = val
    return body or None


def _push_late_fields(entity: str, item_id: str, item: Optional[Dict[str, Any]]) -> None:
    """create 成功后补齐「create 端点不收」的字段。"""
    body = _late_fields_for(entity, item)
    if not body:
        return
    try:
        api_client.push_op("PUT", f"/api/v1/applications/{item_id}", body)
    except api_client.ApiOfflineError as exc:
        # 主记录已建成；这一步没走成 → 重新入队，下一轮再补（不丢）
        local_store.save(entity, "update", item_id, dict(item or {}))
        logger.info("[%s] %s 补充字段待下轮同步：%s", entity, item_id, exc)
    except api_client.ApiError as exc:
        # 服务端明确拒绝（如状态值非法）→ 重试无意义，如实记一条日志后放行
        logger.warning("[%s] %s 补充字段被服务端拒绝：%s", entity, item_id, exc)


def push_pending(limit: int = 200) -> Dict[str, int]:
    """上行所有待处理的本地改动。"""
    pushed = failed = skipped = 0
    for entry in local_store.pending_ops(limit):
        spec = _spec_for(entry["entity"], entry["op"], entry["id"], entry["payload"])
        if spec is None:
            # 该实体尚未 P4 化（例如后续新增的实体还没补 _spec_for 分支），跳过；
            # 不标注失败以免污染状态
            skipped += 1
            continue
        method, path, body = spec
        try:
            data = api_client.push_op(method, path, body)
        except api_client.ApiOfflineError as exc:
            # 链路断了：后面的必然也失败，停手，队列原样留给下一轮
            local_store.resolve_op(entry["seq"], False, str(exc))
            logger.info("同步中断（离线），本轮已上行 %d 条，剩余等待下次", pushed)
            break
        except api_client.ApiError as exc:
            # 服务端明确拒绝：记失败但继续，别让一条坏数据卡死整个队列
            local_store.resolve_op(entry["seq"], False, str(exc))
            failed += 1
            logger.warning("[%s] %s 上行被拒：%s", entry["entity"], entry["id"], exc)
            continue
        sid = ""
        if isinstance(data, dict):
            sid = str(data.get("id") or "")
        local_store.resolve_op(entry["seq"], True, server_id=sid)
        pushed += 1
        if entry["op"] == "create":
            _push_late_fields(entry["entity"], entry["id"], entry["payload"])
    return {"pushed": pushed, "failed": failed, "skipped": skipped}


def pull_all() -> Dict[str, int]:
    """下行各实体（失败记 -1，不影响其他实体）。"""
    out: Dict[str, int] = {}
    for entity, fn in _DOWNLOADERS.items():
        try:
            out[entity] = len(fn() or [])
        except Exception as exc:  # noqa: BLE001
            out[entity] = -1
            logger.warning("下行 %s 失败：%s: %s", entity, type(exc).__name__, exc)
    return out


def sync_once() -> Dict[str, Any]:
    """执行一轮双向同步，返回统计（供 UI 展示 / 测试断言）。"""
    if not api_client.is_logged_in():
        return {"ok": False, "error": "未登录", "pushed": 0, "failed": 0, "down": {}}
    up = push_pending()
    down = pull_all()
    # 有失败就如实记下来（下次会重试），不假装成功
    result = "ok" if up["failed"] == 0 else f"failed={up['failed']}"
    local_store.mark_sync_done(result)
    return {"ok": True, "pushed": up["pushed"], "failed": up["failed"],
            "skipped": up["skipped"], "down": down,
            "pending_left": local_store.pending_count()}


class SyncService:
    """后台同步线程：登录后启动，登出停止。

    启动后**先立刻同步一次**（不等满 30 分钟），之后按 SYNC_INTERVAL 周期执行。
    """

    def __init__(self, interval: int = SYNC_INTERVAL,
                 on_result: Optional[Callable[[Dict[str, Any]], None]] = None):
        self.interval = interval
        self._on_result = on_result
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.last_result: Dict[str, Any] = {}

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="offerflow-sync", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    def kick(self) -> None:
        """请求立刻同步一次（重启计时）。"""
        self._wake.set()

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                if api_client.is_logged_in():
                    res = sync_once()
                    self.last_result = res
                    if self._on_result is not None:
                        self._on_result(res)
            except Exception as exc:  # noqa: BLE001
                # 同步是后台增强能力，任何异常都不该把线程弄死
                logger.warning("同步轮次异常：%s: %s", type(exc).__name__, exc)
            self._wake.wait(self.interval)
            self._wake.clear()


_default: Optional[SyncService] = None
_default_lock = threading.Lock()


def service() -> SyncService:
    """进程内单例（UI 与 main 共用）。"""
    global _default
    with _default_lock:
        if _default is None:
            _default = SyncService()
        return _default


__all__ = ["SYNC_INTERVAL", "SyncService", "service", "sync_once",
           "push_pending", "pull_all"]
