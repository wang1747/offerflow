"""本地只读副本能力测试。

背景：桌面版是纯客户端，业务数据只存服务器。断网时读接口返回空列表，
界面显示「暂无数据」，用户会误以为数据全丢。本轮新增 `local_cache` 只读副本
+ `api_client` 异常分层（ApiOfflineError / ApiAuthError），让「断网」与
「真无数据」「登录失效」三种状态在语义上可区分，断网时能读到本地副本。

本测试不依赖真实网络：mock 底层 `requests.request`（而非 `_request`），
让 `_request` 的真实逻辑（含 `_mark_offline` 熔断、401 分类）完整走通。
local_cache 层指向临时 SQLite 文件。覆盖 5 项验收：
  1. 三态可区分（真无数据 / 离线 / 登录失效 返回不同语义）
  2. 断网读到副本，且内容与联网时一致
  3. 写操作断网时明确抛错（不再静默成功）
  4. 副本按账号隔离，不串号
  5. 副本可导出（entities/stats）

运行：python tests/test_local_cache.py
"""

import os
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, ROOT)

import requests  # noqa: E402

from offerflow_desktop import api_client  # noqa: E402
from offerflow_desktop import local_cache  # noqa: E402

# 隔离：把 local_cache 的库文件指到临时目录，绝不污染真实 DATA_DIR
_TMP = tempfile.mkdtemp(prefix="offflow_cache_test_")
local_cache._DB_PATH = os.path.join(_TMP, "test_cache.db")

_SAMPLE_APPS = [
    {"id": "app-1", "company": "字节跳动", "position": "后端开发", "status": "applied"},
    {"id": "app-2", "company": "腾讯", "position": "AI 应用", "status": "interview"},
]


def _reset_state():
    local_cache.clear("*")
    local_cache.set_account(None)
    api_client._state["offline"] = False
    api_client._state["offline_reason"] = ""
    api_client._state["offline_since"] = None
    api_client._state["offline_until"] = 0.0
    api_client._state["served_from_cache"] = False


def _resp(status: int, data=None):
    """构造一个带 status_code 和 json() 的假 HTTP 响应。"""
    r = mock.MagicMock()
    r.status_code = status
    r.json.return_value = data
    return r


class TestLocalCacheLayer(unittest.TestCase):
    """local_cache 纯层测试（不碰网络）。"""

    def setUp(self):
        _reset_state()

    def test_mirror_read_roundtrip(self):
        local_cache.set_account("a@test.com")
        ok = local_cache.mirror("applications", _SAMPLE_APPS)
        self.assertTrue(ok)
        self.assertEqual(local_cache.read("applications"), _SAMPLE_APPS)
        self.assertIsNotNone(local_cache.synced_at("applications"))

    def test_account_isolation(self):
        """账号 A 存的副本，账号 B 读不到（不串号）。"""
        local_cache.set_account("a@test.com")
        local_cache.mirror("applications", _SAMPLE_APPS)
        local_cache.set_account("b@test.com")
        self.assertIsNone(local_cache.read("applications"))
        self.assertEqual(local_cache.entities(), [])
        local_cache.set_account("a@test.com")
        self.assertEqual(local_cache.read("applications"), _SAMPLE_APPS)

    def test_mirror_requires_account(self):
        """未登录（无账号）时不写副本。"""
        local_cache.set_account(None)
        self.assertFalse(local_cache.mirror("applications", _SAMPLE_APPS))
        self.assertIsNone(local_cache.read("applications"))

    def test_clear_and_export(self):
        local_cache.set_account("a@test.com")
        local_cache.mirror("applications", _SAMPLE_APPS)
        ents = local_cache.entities()
        self.assertEqual(len(ents), 1)
        self.assertEqual(ents[0]["entity"], "applications")
        self.assertEqual(ents[0]["count"], 2)
        stats = local_cache.stats()
        self.assertEqual(stats["item_total"], 2)
        self.assertEqual(stats["entity_count"], 1)
        n = local_cache.clear()
        self.assertEqual(n, 1)
        self.assertIsNone(local_cache.read("applications"))


class TestApiClientThreeStates(unittest.TestCase):
    """api_client 层三态区分（mock 底层 requests.request）。"""

    def setUp(self):
        _reset_state()

    def test_truly_empty_returns_empty_not_cache(self):
        """联网成功且真无数据：返回 []，且不标记 served_from_cache。"""
        with mock.patch.object(requests, "request",
                               return_value=_resp(200, {"success": True, "data": []})):
            got = api_client.list_applications()
        self.assertEqual(got, [])
        self.assertFalse(api_client.served_from_cache())
        self.assertFalse(api_client.is_offline())

    def test_offline_returns_cache_and_marks(self):
        """离线：返回本地副本，且 served_from_cache 置位、offline 置位。"""
        local_cache.set_account("a@test.com")
        local_cache.mirror("applications", _SAMPLE_APPS)
        with mock.patch.object(requests, "request",
                               side_effect=requests.exceptions.ConnectionError()):
            got = api_client.list_applications()
        self.assertEqual(got, _SAMPLE_APPS)
        self.assertTrue(api_client.served_from_cache())
        self.assertTrue(api_client.is_offline())

    def test_offline_no_cache_returns_empty_but_still_offline(self):
        """离线且无副本：返回 []，但 offline 标志仍在（界面可提示「本地无副本」）。"""
        with mock.patch.object(requests, "request",
                               side_effect=requests.exceptions.ConnectionError()):
            got = api_client.list_applications()
        self.assertEqual(got, [])
        self.assertTrue(api_client.is_offline())

    def test_auth_error_does_not_fall_back_to_cache(self):
        """登录失效：绝不回退本地副本（副本可能属于上一个账号）。"""
        local_cache.set_account("a@test.com")
        local_cache.mirror("applications", _SAMPLE_APPS)
        with mock.patch.object(requests, "request",
                               return_value=_resp(401, {"detail": "登录已失效"})):
            got = api_client.list_applications()
        self.assertEqual(got, [])
        self.assertFalse(api_client.served_from_cache())


class TestApiClientOfflineWrite(unittest.TestCase):
    """写操作断网时明确抛错（不再静默成功）。"""

    def setUp(self):
        _reset_state()

    def _offline(self):
        return mock.patch.object(requests, "request",
                                 side_effect=requests.exceptions.ConnectionError())

    def test_delete_application_raises(self):
        with self._offline():
            with self.assertRaises(api_client.ApiOfflineError):
                api_client.delete_application("app-1")

    def test_update_application_raises(self):
        with self._offline():
            with self.assertRaises(api_client.ApiOfflineError):
                api_client.update_application("app-1", status="interview")

    def test_add_application_event_raises(self):
        with self._offline():
            with self.assertRaises(api_client.ApiOfflineError):
                api_client.add_application_event("app-1", "备注")

    def test_delete_resume_raises(self):
        with self._offline():
            with self.assertRaises(api_client.ApiOfflineError):
                api_client.delete_resume("resume-1")


class TestApiClientMirrorOnSuccess(unittest.TestCase):
    """联网成功时把结果镜像到本地。"""

    def setUp(self):
        _reset_state()

    def test_success_mirrors_payload(self):
        local_cache.set_account("a@test.com")
        with mock.patch.object(requests, "request",
                               return_value=_resp(200, {"success": True,
                                                        "data": _SAMPLE_APPS})):
            got = api_client.list_applications()
        self.assertEqual(got, _SAMPLE_APPS)
        # 成功读的结果应已镜像到本地
        self.assertEqual(local_cache.read("applications"), _SAMPLE_APPS)


if __name__ == "__main__":
    unittest.main(verbosity=2)
