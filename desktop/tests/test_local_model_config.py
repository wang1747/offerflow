"""本地模型配置（本地知识库的图谱 / 记忆用）—— 语义、边界、接线与反向对照。

背景（2026-10-07 用户报障「抽取图谱和提炼记忆一直提示配置 API，但我配过了」）：

    桌面版此前有**两套**模型配置，且都不通：

      · 「模型设置」页 → `api_client` → **云端账号** `/api/v1/user/model-config/`
        （Key 服务端加密、只回掩码）；
      · 「本地知识库」抽取图谱 / 提炼记忆 → `database.get_model_config()`
        → **本地** SQLite `settings` 表。

    而写本地那份的 `database.save_model_config()` **从来没有调用点** ⇒ 本地表恒空
    ⇒ 用户怎么配都提示「未配置模型」。

    修法：①「模型设置」保存成功后同步写一份到 `local_model.*`（双写）；
         ②「本地知识库」页加「本地模型…」入口，可单独配（支持无 Key 的本机 Ollama）。

本用例的三层判据：
    · 语义层 —— 写进去能读出来、清空后回落、**无 Key 也能用**（Ollama）；
    · 边界层 —— 不联网、不污染旧的 `model.*` 语义；
    · 接线层 —— 保存路径确实双写、页面确实有入口（AST 断言，不构造 Qt）。
反向对照：屏蔽「读 local_model」这一步（等价于老代码），用例必须变红。

运行：python tests/test_local_model_config.py
"""

import ast
import os
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, ROOT)

from offerflow_desktop import database  # noqa: E402
from offerflow_desktop.local_ai import graph as kg, memory as mem  # noqa: E402

_URL = "http://127.0.0.1:11434/v1"
_MODEL = "qwen2.5:7b"

_SETTINGS_DDL = ("CREATE TABLE IF NOT EXISTS settings "
                 "(key TEXT PRIMARY KEY, value TEXT NOT NULL)")


class _IsolatedDb(unittest.TestCase):
    """每个用例一个独立 SQLite 文件 —— 绝不碰真实 DATA_DIR。

    只建本用例需要的 `settings` 表（不调 `init_db()`：它还会去建 users.db，
    没必要为一条配置用例引入副作用）。
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="offflow_localmodel_")
        database._conn = None
        database._conn_path = None
        database._active_user = None
        database.DB_PATH = os.path.join(self.tmp, "isolated.db")
        conn = database.get_conn()
        conn.execute(_SETTINGS_DDL)
        conn.commit()

    def tearDown(self):
        try:
            if database._conn is not None:
                database._conn.close()
        except Exception:  # noqa: BLE001
            pass
        database._conn = None
        database._conn_path = None


# ==================== 语义层 ====================

class TestSemantics(_IsolatedDb):

    def test_empty_is_unconfigured(self):
        """基线：干净库上必须判「未配置」（这正是用户看到的现象）。"""
        self.assertFalse(database.get_model_config()["has_config"])
        self.assertFalse(kg.available()["ok"])
        self.assertFalse(mem.available()["ok"])

    def test_write_then_read_points_to_same_place(self):
        """核心：写进去，读得到 —— 读端与写端必须是同一处。"""
        database.save_local_model_config("local", _URL, _MODEL, "sk-test-123456")
        cfg = database.get_model_config()
        self.assertTrue(cfg["has_config"])
        self.assertEqual(cfg["base_url"], _URL)
        self.assertEqual(cfg["model"], _MODEL)
        self.assertEqual(cfg["api_key"], "sk-test-123456")
        self.assertEqual(cfg["source"], "local_model")
        self.assertTrue(kg.available()["ok"])
        self.assertTrue(mem.available()["ok"])

    def test_api_key_may_be_empty_for_local_ollama(self):
        """本机 Ollama 没有 Key 也必须可用（本次顺带修的语义）。"""
        database.save_local_model_config("local", _URL, _MODEL, "")
        cfg = database.get_model_config()
        self.assertTrue(cfg["has_config"])
        self.assertEqual(cfg["api_key"], "")
        self.assertTrue(kg.available()["ok"])

    def test_clear_returns_to_unconfigured(self):
        database.save_local_model_config("local", _URL, _MODEL, "")
        database.clear_local_model_config()
        self.assertFalse(database.get_model_config()["has_config"])
        self.assertFalse(kg.available()["ok"])

    def test_switch_to_keyless_model_drops_old_key(self):
        """从云端模型切到本机 Ollama：旧 Key 不许残留。

        （`save_local_model_config` 与 `save_model_config` 的关键差异就是
        「空 Key 也要落库」—— 老逻辑「空值不覆盖」会把旧 Key 一直留着。）
        """
        database.save_local_model_config(
            "deepseek", "https://api.deepseek.com", "deepseek-chat", "sk-old-1234567890")
        database.save_local_model_config("local", _URL, _MODEL, "")
        cfg = database.get_model_config()
        self.assertEqual(cfg["api_key"], "")
        self.assertEqual(cfg["base_url"], _URL)
        self.assertTrue(cfg["has_config"])

    def test_local_wins_over_legacy(self):
        """两处都有时，本地这份优先（它是用户最近一次在「模型设置」保存时同步的）。"""
        database.save_model_config(
            "deepseek", "https://api.deepseek.com", "deepseek-chat", "sk-legacy-123456")
        database.save_local_model_config("local", _URL, _MODEL, "")
        cfg = database.get_model_config()
        self.assertEqual(cfg["base_url"], _URL)
        self.assertEqual(cfg["source"], "local_model")

    def test_legacy_model_star_still_readable(self):
        """老安装里可能只有 `model.*` —— 回退分支仍要读得出来（兼容）。"""
        database.save_model_config(
            "deepseek", "https://api.deepseek.com", "deepseek-chat", "sk-legacy-123456")
        cfg = database.get_model_config()
        self.assertTrue(cfg["has_config"])
        self.assertEqual(cfg["source"], "model")
        self.assertTrue(kg.available()["ok"])

    def test_available_reports_source(self):
        """页面要能区分「本机配置」与「账号同步」，所以 available 得带 source。"""
        database.save_local_model_config("local", _URL, _MODEL, "")
        self.assertEqual(kg.available()["source"], "local_model")
        self.assertEqual(mem.available()["source"], "local_model")

    def test_model_settings_sync_local_writes(self):
        """行为验证（不只是接线断言）：保存时调用的 `_sync_local` 真把配置写进了本地。"""
        from offerflow_desktop.ui.pages import model_settings

        note = model_settings._sync_local({
            "provider": "deepseek",
            "base_url": "https://api.deepseek.com",
            "model": "deepseek-chat",
            "api_key": "sk-sync-123456",
        })
        self.assertEqual(note, "")
        cfg = database.get_model_config()
        self.assertTrue(cfg["has_config"])
        self.assertEqual(cfg["api_key"], "sk-sync-123456")
        self.assertEqual(cfg["source"], "local_model")
        self.assertTrue(kg.available()["ok"])


# ==================== 边界层 ====================

class TestBoundaries(_IsolatedDb):

    def test_no_network_touched(self):
        """这条路必须是纯本地的 —— 联网的是「模型设置」那条云端路径。"""
        boom = AssertionError("本地模型配置路径不应发起网络请求")
        with mock.patch("requests.request", side_effect=boom), \
                mock.patch("requests.post", side_effect=boom), \
                mock.patch("requests.get", side_effect=boom):
            database.save_local_model_config("local", _URL, _MODEL, "")
            self.assertTrue(database.get_model_config()["has_config"])
            self.assertTrue(kg.available()["ok"])
            self.assertTrue(mem.available()["ok"])
            database.clear_local_model_config()
            self.assertFalse(database.get_model_config()["has_config"])

    def test_reverse_control_legacy_read_path_would_fail(self):
        """反向对照：屏蔽「读 local_model」（= 老代码的行为），必须变红。

        证明上面那些 `ok=True` 的断言确实依赖新读端，
        而不是「怎么写都通过」的空断言。
        """
        database.save_local_model_config("local", _URL, _MODEL, "sk-test-123456")
        legacy = {"provider": "", "base_url": "", "model": "", "api_key": "",
                  "api_key_masked": "", "has_config": False, "source": "model"}
        with mock.patch.object(database, "get_local_model_config", return_value=legacy):
            self.assertFalse(database.get_model_config()["has_config"])  # 老行为：读不到
            self.assertFalse(kg.available()["ok"])                       # 老行为：报未配置
            self.assertFalse(mem.available()["ok"])

    def test_reverse_control_require_key_would_fail(self):
        """反向对照二：把判据改回「必须有 Key」，无 Key 的 Ollama 场景必须变红。"""
        database.save_local_model_config("local", _URL, _MODEL, "")
        real = database.get_local_model_config()
        self.assertTrue(real["has_config"])
        old_rule = dict(real, has_config=bool(real["api_key"] and real["base_url"]
                                              and real["model"]))
        self.assertFalse(old_rule["has_config"])


# ==================== 行为层：无 Key 时不带 Authorization ====================

class TestKeylessHeader(_IsolatedDb):

    @staticmethod
    def _fake_post(captured):
        def _post(url, json=None, headers=None, timeout=None):  # noqa: A002
            captured["headers"] = dict(headers or {})
            captured["url"] = url
            resp = mock.MagicMock()
            resp.status_code = 200
            resp.json.return_value = {"choices": [{"message": {"content": "ok"}}]}
            return resp
        return _post

    def _chat(self, api_key: str) -> dict:
        from offerflow_desktop import llm

        database.save_local_model_config("local", _URL, _MODEL, api_key)
        captured: dict = {}
        with mock.patch("requests.post", side_effect=self._fake_post(captured)):
            self.assertEqual(llm.chat("系统", "用户"), "ok")
        return captured

    def test_keyless_request_omits_authorization(self):
        """Ollama 这类内网模型：空 Key 时不能发 'Bearer '（部分实现会 401）。"""
        captured = self._chat("")
        self.assertEqual(captured["url"], _URL + "/chat/completions")
        self.assertNotIn("Authorization", captured["headers"])

    def test_keyed_request_sends_bearer(self):
        captured = self._chat("sk-abc123456")
        self.assertEqual(captured["headers"].get("Authorization"), "Bearer sk-abc123456")


# ==================== 接线层（AST，不构造 Qt） ====================

def _dotted(node) -> str:
    """把 `a.b.c(...)` 的调用目标还原成 'a.b.c'。"""
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
    return ".".join(reversed(parts))


def _calls(node) -> set:
    return {_dotted(n.func) for n in ast.walk(node) if isinstance(n, ast.Call)}


def _attrs(node) -> set:
    return {n.attr for n in ast.walk(node) if isinstance(n, ast.Attribute)}


class TestWiring(unittest.TestCase):
    """接线判据：保存路径真的双写了吗？页面真的有入口吗？

    用 AST 而不是构造 Qt —— 本测试不引入 QApplication，
    避免拖进平台的窗口退出码问题（错题本 W-01）。
    """

    def _tree(self, rel: str):
        path = os.path.join(ROOT, "offerflow_desktop", rel)
        with open(path, "r", encoding="utf-8") as f:
            return ast.parse(f.read())

    def _func(self, rel: str, name: str):
        for node in ast.walk(self._tree(rel)):
            if isinstance(node, ast.FunctionDef) and node.name == name:
                return node
        self.fail(f"{rel} 里找不到函数 {name}")

    def test_model_settings_save_dual_writes(self):
        """`_on_save` → `_sync_local` → `local_db.save_local_model_config`。"""
        self.assertIn("_sync_local", _calls(self._func("ui/pages/model_settings.py", "_on_save")))
        sync = self._func("ui/pages/model_settings.py", "_sync_local")
        self.assertIn("local_db.save_local_model_config", _calls(sync))

    def test_model_settings_clear_also_clears_local(self):
        self.assertIn("local_db.clear_local_model_config",
                      _calls(self._func("ui/pages/model_settings.py", "_on_clear")))

    def test_local_kb_has_model_entry(self):
        card = self._func("ui/pages/local_kb.py", "_kg_card")
        self.assertIn("kg_model_btn", _attrs(card))
        opener = self._func("ui/pages/local_kb.py", "_open_local_model")
        self.assertIn("LocalModelDialog", _calls(opener))

    def test_local_model_dialog_writes_local(self):
        saver = self._func("ui/local_model_dialog.py", "_on_save")
        self.assertIn("local_db.save_local_model_config", _calls(saver))
        cleaner = self._func("ui/local_model_dialog.py", "_on_clear")
        self.assertIn("local_db.clear_local_model_config", _calls(cleaner))

    def test_database_exposes_local_api(self):
        for name in ("get_local_model_config", "save_local_model_config",
                     "clear_local_model_config"):
            self.assertTrue(hasattr(database, name), f"database 缺少 {name}")

    def test_local_model_dialog_load_stays_local(self):
        """`_load` 被登记进「构造期安全」白名单，前提是它**只读本地配置**。

        这条护栏与白名单互锁：哪天有人往 `_load` 里塞了网络 / 线程调用，
        白名单仍会放行（构造期异步审计看不见），但这里会红。
        """
        calls = _calls(self._func("ui/local_model_dialog.py", "_load"))
        self.assertIn("local_db.get_local_model_config", calls)
        for banned in ("LLMWorker", "requests.get", "requests.post", "requests.request",
                       "api.get_model_config", "api_client.get_model_config"):
            self.assertNotIn(banned, calls, f"_load 里不该出现 {banned}（它必须是纯本地读）")


if __name__ == "__main__":
    unittest.main(verbosity=2)
