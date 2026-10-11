#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Agent 左栏会话列表（对齐网页版 AgentConsole 的 localStorage 那份）。

对标的网页版行为
----------------
1. 会话列表本机持久化，**刷新/重开页面不丢**（网页用 localStorage，
   桌面用 SQLite 的 drafts 表 —— 本机行为，不该进 local_store 同步队列）。
2. 最新置顶、按 session_id 去重、最多 20 条。
3. 点历史条目 → 按它恢复当前会话；列表里**不**刷新它的置顶时间（切过去看≠新活动）。
4. 「新建」只清当前视图，**不清空已保存的历史**。

判据全部落在「控件树 / 往返一致 / 顺序」上，而不是「源码里写了这行」。
每条正向判据都配一条反向对照（把缺陷放回去，判据必须变红）：

  · 缺 session_id 的会话不得进列表（否则会存出点不开的幽灵条目）；
  · 恢复历史时不得刷新置顶时间（否则一浏览就把顺序打乱）；
  · 「新建」不得连带删历史（否则用户点一次新建就丢光记录）。

⚠️ 本文件刻意**不用**「连打 25 次 `_on_session_created`」来验数量上限：
   那条路每打一次会起 2 个 QThread（待确认 + 记忆各一），本机 Qt/PySide6 在
   offscreen 下**快速连续创建 ~25 个 QThread 会间歇性段错误（exit 139）**，
   与页面代码无关（一个空控件 + 纯 `LLMWorker` 循环也能复现）。上限/排序属于
   数据层契约，直接打 `_remember_session` 即可，且不会引入这种环境抖动。
   真实链路（`_on_session_created` → 落盘 → 出行）另有一条集成判据覆盖。

运行：python tests/test_agent_sessions.py
"""

import os
import sys
import tempfile
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from PySide6.QtGui import QShowEvent                               # noqa: E402
from PySide6.QtWidgets import QApplication, QPushButton, QWidget      # noqa: E402

_APP = QApplication.instance() or QApplication([])

from offerflow_desktop import database                                 # noqa: E402
from offerflow_desktop.ui.pages import agent as agent_mod              # noqa: E402
from offerflow_desktop.ui.pages.agent import AgentPage                 # noqa: E402


class _FakeMW(QWidget):
    """AgentPage 构造期可能回调的宿主接口（同 test_agent_wiring 的做法）。"""

    def __init__(self):
        super().__init__()

    def open_agent(self, entry):
        pass

    def refresh_status(self):
        pass

    def refresh_ai_source(self):
        pass

    def navigate(self, *a, **k):
        pass

    def set_status(self, *a, **k):
        pass

    def reapply_icons(self, *a, **k):
        pass


def _session(sid, company="字节", position="后端", resume="张三简历", version=1):
    """造一份最小可用的 AgentSession（只保留会话列表需要的字段）。"""
    return {
        "session_id": sid,
        "thread_id": sid,
        "checkpointer": "postgres",
        "memory_enabled": False,
        "run_count": 0,
        "context_refs": [
            {"type": "jd", "id": f"jd-{sid}", "label": company},
            {"type": "resume", "id": f"r-{sid}", "label": resume},
        ],
        "missing_context": [],
        "_names": (company, position, resume, version),   # 测试自带，供桩解析
    }


class _IsolatedDb(unittest.TestCase):
    """每个用例一个独立 SQLite 文件，绝不碰真实 DATA_DIR。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="offflow_agent_sessions_")
        self._saved = (database.DB_PATH, database._conn,
                       database._conn_path, database._active_user)
        database._conn = None
        database._conn_path = None
        database._active_user = None
        database.DB_PATH = os.path.join(self.tmp, "isolated.db")
        database.init_db()
        self.addCleanup(self._restore)

    def _restore(self):
        (database.DB_PATH, database._conn,
         database._conn_path, database._active_user) = self._saved

    # ---- 造页 ----
    def _page(self):
        page = AgentPage(_FakeMW())
        self.addCleanup(page.setParent, None)
        self._jds = {}
        self._resumes = {}

        def _list_jds():
            return list(self._jds.values())

        def _list_resumes():
            return list(self._resumes.values())

        agent_mod.api.list_jds = _list_jds
        agent_mod.api.list_resumes = _list_resumes
        agent_mod.api.list_applications = lambda: []
        # 会话创建后会顺手刷「待确认写操作」与「长期记忆」——离线桩掉，
        # 否则单测会去连真实服务端（沙箱里会挂起/被拦）。
        agent_mod.api.agent_list_actions = lambda _sid: []
        agent_mod.api.agent_list_memories = lambda: []
        page._run_job = self._sync_run(page)
        return page

    @staticmethod
    def _sync_run(page):
        """``_run_job`` 的**同步替身**：不起 QThread，走同一条回调链。

        为什么必须替掉：`_run_job` 每次都 new 一个 ``LLMWorker``（QThread）。
        这里的桩全是同步返回的，线程毫无收益，却踩中本机 Qt/PySide6 在
        offscreen 下的已知不稳定点 —— 实测 **8 次里约 1 次**在
        ``LLMWorker.__init__`` 里 access violation（exit 139）。已用
        ``-X faulthandler`` 抓到完整栈：

            llm.py:162 __init__
            agent.py:608 _run_job
            agent.py:837 _refresh_pending
            agent.py:779 _on_session_loaded
            agent.py:1000 _on_session_restored
            tests/test_agent_sessions.py:166 _pump

        崩溃点在**主线程**、由 ``_pump()`` 处理排队回调时触发，与页面逻辑
        无关（一个空控件 + 纯 ``LLMWorker`` 循环也能复现）。数据层判据不该
        因为这种环境抖动随机变红 —— 发版门槛是全量退出码 0，随机崩会直接
        污染这个信号。
        """
        def _run(fn, on_ok, on_err=None, memory=False):
            # 与 `_run_job` 保持同样的可观察行为：busy 状态 + 两条回调
            if memory:
                page._set_memory_busy(True)
            else:
                page._set_busy(True)
            try:
                result = fn()
            except Exception as exc:                      # noqa: BLE001
                if memory:
                    page._set_memory_busy(False)
                else:
                    page._set_busy(False)
                if on_err is not None:
                    on_err(str(exc))
                else:
                    page._show_error(str(exc))
                return
            if memory:
                page._set_memory_busy(False)
            else:
                page._set_busy(False)
            on_ok(result)

        return _run

    def _feed_contexts(self, page, sessions):
        """把会话里引用的 JD / 简历灌进候选，让名称解析得到「公司 · 职位」。"""
        for s in sessions:
            company, position, resume, version = s["_names"]
            self._jds[f"jd-{s['session_id']}"] = {
                "id": f"jd-{s['session_id']}", "company": company, "position": position}
            self._resumes[f"r-{s['session_id']}"] = {
                "id": f"r-{s['session_id']}", "source_file": resume, "version": version}
        page._jd_list = list(self._jds.values())
        page._resume_list = list(self._resumes.values())

    def _visible(self, page, sessions):
        """准备好页面：灌上下文 + 落会话（走数据层，不起线程）。"""
        self._feed_contexts(page, sessions)
        for s in sessions:
            page._remember_session(s)
        return page

    # ---- 判据工具 ----
    def _session_buttons(self, page):
        """控件树里真正的会话行（按 class 属性筛，不依赖内部列表长度）。"""
        out = []
        box = page._sessions_box
        for i in range(box.count()):
            w = box.itemAt(i).widget()
            if isinstance(w, QPushButton) and w.property("class") == "session-item":
                out.append(w)
        return out

    def _pump(self):
        """跑完队列里的后台任务（桩都是同步返回的，排几次事件即可）。"""
        from offerflow_desktop.llm import _ALIVE
        for _ in range(200):
            _APP.processEvents()
            if not _ALIVE:
                break

    def _show(self, page):
        """让页面真正可见。

        为什么非做不可：``QPushButton.click()`` 在**不可见**的控件上不会发 clicked
        （Qt 的行为，实测 hits=[] 而 ``clicked.emit()`` 有反应）。要断言「点按钮的
        真实效果」，就必须先 show，不能只 emit 信号 —— 后者绕过了 enabled 判断。
        """
        page.show()
        _APP.processEvents()
        self._pump()


class TestAgentSessionPersistence(_IsolatedDb):

    def test_sync_runner_delivers_the_success_callback(self):
        """替身必须保留 `_run_job` 的语义（否则它被写成 no-op 也没人发现）。"""
        page = self._page()
        seen = []
        page._run_job(lambda: 42, seen.append)
        self.assertEqual(seen, [42])

    def test_sync_runner_delivers_the_error_callback(self):
        page = self._page()
        errs = []

        def _boom():
            raise RuntimeError("模拟失败")

        page._run_job(_boom, lambda _r: None, on_err=errs.append)
        self.assertEqual(errs, ["模拟失败"])

    def test_no_real_worker_thread_is_started(self):
        """跑真实链路后不该有活着的 LLMWorker（本机 Qt 会随机段错误）。"""
        from offerflow_desktop.llm import _ALIVE
        page = self._page()
        s = _session("s1", company="字节跳动", position="后端实习")
        self._feed_contexts(page, [s])
        page._on_session_created(s)
        self._pump()
        self.assertEqual(list(_ALIVE), [], "装置起了真实 QThread")

    def test_empty_db_reads_as_no_sessions(self):
        self.assertEqual(agent_mod._load_sessions(), [],
                         "空库应读成「还没有保存的会话」而不是抛异常")

    def test_session_created_writes_history(self):
        """集成判据：真实链路（创建会话 → 落盘）不能断在 `_remember_session` 之前。"""
        page = self._page()
        s = _session("s1", company="字节跳动", position="后端实习")
        self._feed_contexts(page, [s])
        page._on_session_created(s)
        self._pump()
        saved = agent_mod._load_sessions()
        self.assertEqual([x["session_id"] for x in saved], ["s1"],
                         "创建会话必须落到本机历史里")

    def test_context_label_resolves_company_position_and_version(self):
        page = self._page()
        s = _session("s1", company="字节跳动", position="后端实习")
        self._visible(page, [s])
        saved = agent_mod._load_sessions()
        self.assertIn("字节跳动 · 后端实习", saved[0]["context"],
                      "上下文摘要应能解析出「公司 · 职位」")
        self.assertIn("v1", saved[0]["context"], "简历应带版本号")

    def test_dedup_and_newest_first(self):
        page = self._page()
        s1, s2 = _session("s1"), _session("s2")
        self._visible(page, [s1, s2])
        page._remember_session(s1)            # 再回 s1：应置顶且去重
        self.assertEqual([x["session_id"] for x in page._local_sessions],
                         ["s1", "s2"], "重复的 session_id 必须去重并置顶")

    def test_capped_at_20(self):
        page = self._page()
        for i in range(25):
            page._remember_session(_session(f"s{i}"))
        self.assertEqual(len(page._local_sessions), agent_mod._SESSIONS_MAX)
        self.assertEqual(page._local_sessions[0]["session_id"], "s24", "最新应置顶")
        self.assertEqual(len(agent_mod._load_sessions()), agent_mod._SESSIONS_MAX,
                         "上限要落到盘上，不只是内存里截断")

    def test_ghost_session_without_id_is_not_saved(self):
        """反向对照：没有 session_id 的会话不得进列表（否则存出点不开的幽灵条目）。"""
        page = self._page()
        page._remember_session({"context_refs": []})
        page._remember_session({})
        page._remember_session(None)
        self.assertEqual(page._local_sessions, [])
        self.assertEqual(agent_mod._load_sessions(), [], "不得写盘")

    def test_survives_page_rebuild(self):
        """刷新/重开不丢 —— 换一个新页面实例，历史仍在。"""
        page = self._page()
        self._visible(page, [_session("s1")])
        page2 = self._page()
        page2.showEvent(QShowEvent())          # 读盘发生在 showEvent，不在构造期
        self._pump()
        self.assertEqual([x["session_id"] for x in page2._local_sessions], ["s1"],
                         "重开页面应从本机库读回历史")

    def test_ctor_does_not_read_disk(self):
        """构造期**不读盘**：本项目的硬规矩（构造只搭界面）。

        这条同时挡住「在 __init__ 里做 IO」——那会在 logout→relogin 重建
        MainWindow 时卡住或崩进程（`test_ctor_async_audit` 的静态审计也管这条）。
        """
        page = self._page()
        self._visible(page, [_session("s1")])      # 盘上已有 1 条
        fresh = self._page()
        self.assertEqual(fresh._local_sessions, [],
                         "构造期不该读盘（读盘必须发生在 showEvent）")
        fresh.showEvent(QShowEvent())
        self._pump()
        self.assertEqual(len(fresh._local_sessions), 1, "showEvent 后应读回来")

    def test_show_event_merges_and_does_not_drop_just_created(self):
        """幂等合并：showEvent 二次触发（或建会话后才 show）不得顶掉内存里那份。"""
        page = self._page()
        self._feed_contexts(page, [_session("s0")])
        page._remember_session(_session("s0"))     # 先建（模拟 open_with 早于 show）
        page.showEvent(QShowEvent())
        self._pump()
        page.showEvent(QShowEvent())               # 再 show 一次
        self._pump()
        ids = [x["session_id"] for x in page._local_sessions]
        self.assertEqual(ids, ["s0"], "合并后不该重复也不该丢：%s" % ids)

    def test_does_not_touch_the_sync_outbox(self):
        """会话列表是本机行为：不得写进 local_store 的同步 outbox。"""
        page = self._page()
        page._remember_session(_session("s1"))
        from offerflow_desktop import local_store
        self.assertEqual(local_store.pending_count(), 0,
                         "会话列表不该产生任何待上行操作")


class TestAgentSessionUI(_IsolatedDb):

    def test_empty_shows_placeholder_not_buttons(self):
        page = self._page()
        page._render_sessions()
        self.assertEqual(self._session_buttons(page), [])
        texts = [page._sessions_box.itemAt(i).widget().text()
                 for i in range(page._sessions_box.count())
                 if hasattr(page._sessions_box.itemAt(i).widget(), "text")]
        self.assertTrue(any("还没有保存的会话" in t for t in texts),
                        "空列表要有占位提示：%s" % texts)

    def test_rows_match_saved_count_and_carry_session_id(self):
        page = self._page()
        self._visible(page, [_session("s1"), _session("s2")])
        btns = self._session_buttons(page)
        self.assertEqual(len(btns), 2, "控件树里应有 2 行会话")
        self.assertEqual([b.toolTip() for b in btns], ["s2", "s1"],
                         "行顺序应与列表一致（最新置顶），toolTip 带 session_id")

    def test_current_session_is_highlighted(self):
        page = self._page()
        self._visible(page, [_session("s1"), _session("s2")])
        page._session = {"session_id": "s1"}
        page._render_sessions()
        active = [b.toolTip() for b in self._session_buttons(page)
                  if b.property("active") == "true"]
        self.assertEqual(active, ["s1"], "当前会话那一行要高亮")

    def test_new_button_clears_view_but_keeps_history(self):
        """「新建」只清当前视图 —— 历史必须留着（反向对照：删了就丢记录）。"""
        page = self._page()
        self._visible(page, [_session("s1")])
        page._session = {"session_id": "s1"}
        page._render_sessions()
        self._show(page)
        page._new_btn.click()
        self.assertIsNone(page._session, "「新建」应清掉当前会话")
        self.assertEqual(len(page._local_sessions), 1, "历史不能被「新建」删掉")
        self.assertEqual(len(self._session_buttons(page)), 1)


class TestAgentSessionRestore(_IsolatedDb):

    def setUp(self):
        super().setUp()
        self.page = self._page()
        s1, s2 = _session("s1"), _session("s2")
        self._visible(self.page, [s1, s2])
        self.page._session = s2
        self._store = {s["session_id"]: s for s in (s1, s2)}
        agent_mod.api.agent_get_session = lambda sid: self._store.get(sid)

    def test_restore_switches_current_session(self):
        self.page._on_restore_session("s1")
        self._pump()
        self.assertIsNotNone(self.page._session)
        self.assertEqual(self.page._session["session_id"], "s1",
                         "点历史条目应按它恢复当前会话")

    def test_restore_does_not_bump_ordering(self):
        """反向对照：恢复历史**不**刷新置顶时间（否则一浏览就把顺序打乱）。"""
        before = [(x["session_id"], x["updated_at"]) for x in self.page._local_sessions]
        self.page._on_restore_session("s1")
        self._pump()
        after = [(x["session_id"], x["updated_at"]) for x in self.page._local_sessions]
        self.assertEqual(after, before, "恢复历史不该改动列表顺序与时间戳")

    def test_load_with_remember_bumps_ordering(self):
        """正向对照（证明上一条不是「根本没生效」）：带 entry 恢复会置顶。"""
        self.assertEqual([x["session_id"] for x in self.page._local_sessions],
                         ["s2", "s1"])
        self.page._on_session_loaded(self._store["s1"], remember=True)
        self.assertEqual([x["session_id"] for x in self.page._local_sessions],
                         ["s1", "s2"], "entry 恢复（remember=True）应把它置顶")

    def test_restore_missing_session_reports_and_keeps_state(self):
        self.page._on_restore_session("不存在")
        self._pump()
        self.assertEqual(self.page._session["session_id"], "s2",
                         "恢复失败不该把当前会话清掉")
        self.assertTrue(self.page._error, "恢复失败要给出提示")


class TestAgentSessionHelpers(unittest.TestCase):

    def test_time_format_is_readable_and_tolerant(self):
        self.assertEqual(agent_mod._format_session_time("2026-10-08T19:35:00"),
                         "10-08 19:35")
        self.assertEqual(agent_mod._format_session_time("2026-10-08T19:35:00.123456"),
                         "10-08 19:35")
        self.assertEqual(agent_mod._format_session_time(""), "")
        self.assertEqual(agent_mod._format_session_time("上古格式"),
                         "上古格式", "认不出就原样显示，不能抛")


def main() -> int:
    loader = unittest.TestLoader()
    suite = loader.loadTestsFromModule(sys.modules[__name__])
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
