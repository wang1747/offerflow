#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""撤销栈 / 未保存草稿 / 生成页表单快照 的判据。

背景
----
生成简历页原来没有撤销，也没有「关掉再打开还在」的草稿 —— 一次误删卡片或
误改一段就要重填。补上之后最容易出的错**不是崩溃**，而是语义错：

  · 撤销栈不去重 ⇒ 输入框每敲一个字都进栈，200 步会被一次连续输入吃光；
  · 快照按**属性名**收集 ⇒ `self.start` / `self.end` 被教育卡与经历卡各用一次，
    按名字收会漏掉一半；
  · 删卡片只 `deleteLater()` ⇒ 控件只是排队等删，`findChildren` 照样拿得到，
    「卡片真的没了」的判据会假绿；
  · 结构对不上时照样贴回 ⇒ 把 A 字段的字塞进 B 字段，比拒绝恢复糟得多。

所以判据全部落在**控件树**与**往返一致性**上，而不是「源码里写了这行」。

三个真实踩过的坑也在这里钉住：
  ① 坏 JSON 的草稿必须当「没有草稿」并清掉，不能每次打开都报错；
  ② 写草稿时**不能**用两个不同的连接读写（上一版 `get_conn().execute()` 后
     又 `get_conn().commit()`，两次拿到不同连接，UPDATE 从未提交）；
  ③ `findChildren` 判据必须先把卡片 `setParent(None)`，不然删了也在树里。

运行：python tests/test_undo_draft.py
"""

import ast
import os
import sys
import tempfile
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from PySide6.QtWidgets import QApplication                        # noqa: E402

_APP = QApplication.instance() or QApplication([])

from offerflow_desktop import database                            # noqa: E402
from offerflow_desktop.core.undo import DEFAULT_LIMIT, UndoStack   # noqa: E402


class TestUndoStack(unittest.TestCase):

    def test_dedup_and_limit(self):
        s = UndoStack(limit=3)
        for v in ("a", "b", "c", "d"):
            s.push(v)
        self.assertEqual(s.current, "d")
        self.assertEqual(s.undo_depth, 3, "超上限后只保留最近 3 步")
        self.assertIs(s.push("d"), False, "相同快照不该进栈（否则一次连打就吃满）")
        self.assertEqual(s.undo_depth, 3)

    def test_undo_redo_are_inverses(self):
        s = UndoStack(limit=3)
        for v in ("a", "b", "c", "d"):
            s.push(v)
        self.assertEqual([s.undo(), s.undo(), s.undo()], ["c", "b", "a"])
        self.assertIsNone(s.undo(), "栈空再撤销返回 None")
        self.assertEqual([s.redo(), s.redo()], ["b", "c"])

    def test_new_push_clears_redo(self):
        s = UndoStack(limit=5)
        s.push("a")
        s.push("b")
        s.undo()
        self.assertTrue(s.can_redo)
        s.push("x")
        self.assertFalse(s.can_redo, "新改动之后旧的重做分支必须作废")
        self.assertEqual(s.current, "x")

    def test_limit_one(self):
        s = UndoStack(limit=1)
        s.push(1)
        s.push(2)
        self.assertEqual(s.undo_depth, 1)

    def test_default_limit_is_bounded(self):
        self.assertIsInstance(DEFAULT_LIMIT, int)
        self.assertGreater(DEFAULT_LIMIT, 0)
        s = UndoStack()
        for i in range(DEFAULT_LIMIT + 50):
            s.push(i)
        self.assertLessEqual(s.undo_depth, DEFAULT_LIMIT,
                             "默认栈不能无界增长（长会话会吃内存）")

    def test_reset(self):
        s = UndoStack(limit=5)
        s.push("a")
        s.push("b")
        s.reset("z")
        self.assertEqual(s.current, "z")
        self.assertFalse(s.can_undo)
        self.assertFalse(s.can_redo)

    def test_snapshot_must_be_hashable_free(self):
        """快照是**纯 dict**（可存盘），所以相同内容的两个 dict 必须判等去重。"""
        s = UndoStack(limit=5)
        self.assertTrue(s.push({"a": 1}))
        self.assertFalse(s.push({"a": 1}), "内容相同的 dict 应被判为重复")


class _IsolatedDb(unittest.TestCase):
    """每个用例一个独立 SQLite 文件，绝不碰真实 DATA_DIR。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="offflow_draft_")
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


class TestDrafts(_IsolatedDb):

    def test_roundtrip(self):
        payload = {"keys": ["QLineEdit:学校#0"],
                   "values": {"QLineEdit:学校#0": "北邮"},
                   "cards": {"exp": 2}, "skills": ["Python"]}
        self.assertTrue(database.save_draft("resume_generate", payload))
        row = database.load_draft("resume_generate")
        self.assertIsNotNone(row)
        self.assertEqual(row["payload"], payload)
        self.assertTrue(row["updated_at"], "要带更新时间，界面才能显示「何时存的」")

    def test_overwrite_not_append(self):
        database.save_draft("s", {"a": 1})
        database.save_draft("s", {"a": 2})
        self.assertEqual(database.load_draft("s")["payload"], {"a": 2})

    def test_scopes_are_independent(self):
        database.save_draft("s1", {"a": 1})
        database.save_draft("s2", {"b": 2})
        self.assertEqual(database.load_draft("s1")["payload"], {"a": 1})
        self.assertEqual(database.load_draft("s2")["payload"], {"b": 2})
        self.assertIsNone(database.load_draft("s3"))

    def test_clear(self):
        database.save_draft("s", {"a": 1})
        database.clear_draft("s")
        self.assertIsNone(database.load_draft("s"))

    def test_broken_json_reads_as_no_draft_and_is_cleaned(self):
        """坏草稿不能每次打开都报错，也不能一直留着。"""
        database.save_draft("s", {"a": 1})
        conn = database.get_conn()
        conn.execute("UPDATE drafts SET payload=? WHERE scope=?", ("{不是 json", "s"))
        conn.commit()
        self.assertIsNone(database.load_draft("s"), "坏 JSON 应读成「没有草稿」")
        self.assertIsNone(database.load_draft("s"), "第二次读也不能抛")

    def test_reverse_write_and_read_use_one_connection(self):
        """写与读必须是同一份数据 —— 双连接会让 UPDATE 静默丢失。

        这条曾经真的踩过：`db.get_conn().execute(...)` 之后又
        `db.get_conn().commit()`，两次调用拿到的是**不同连接**，
        UPDATE 从未提交，于是「写入成功」是假的。
        """
        database.save_draft("s", {"a": 1})
        conn = database.get_conn()
        self.assertIs(conn, database.get_conn(), "同一线程应复用同一连接")
        conn.execute("UPDATE drafts SET payload=? WHERE scope=?", ('{"a": 2}', "s"))
        conn.commit()
        self.assertEqual(database.load_draft("s")["payload"], {"a": 2})

    def test_drafts_are_not_written_to_the_sync_outbox(self):
        """草稿是本机行为，**刻意**不进 local_store 的 outbox。

        进了的话会被「上行云端 + 合并规则」卷入 —— 本地未保存的输入没有任何
        理由离开这台机器，也不该被合并策略改写。判据用 AST：save_draft 体内
        不准出现 local_store。
        """
        with open(os.path.join(_ROOT, "offerflow_desktop", "database.py"),
                  encoding="utf-8") as fh:
            src = fh.read()
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name in (
                    "save_draft", "load_draft", "clear_draft"):
                names = {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}
                attrs = {n.attr for n in ast.walk(node) if isinstance(n, ast.Attribute)}
                self.assertNotIn("local_store", names | attrs,
                                 "%s 不该走同步 outbox" % node.name)

    def test_drafts_table_is_declared_in_schema(self):
        self.assertIn("drafts", database.SCHEMA)


class TestFormSnapshot(_IsolatedDb):

    def setUp(self):
        super().setUp()
        from offerflow_desktop.ui.pages.resume_generate import ResumeGeneratePage
        import types
        self._mw = types.SimpleNamespace(refresh_status=lambda: None)
        self.page = ResumeGeneratePage(self._mw)
        self.addCleanup(self.page.setParent, None)

    # ---- 控件树判据工具 ----

    def _exp_card_count(self) -> int:
        """控件树里「项目/公司名称」输入框的个数 = 经历卡张数。

        为什么用控件树而不是 `len(self._exp_cards)`：后者只能证明**列表**的长度，
        证明不了卡片真的从界面上消失了（`deleteLater` 就是典型反例）。
        """
        return sum(1 for _k, w in self.page._keyed_widgets()
                   if self.page._widget_label(w) == "项目 / 公司名称")

    def test_snapshot_captures_cards_skills_and_values(self):
        self.page.name_edit.setText("张三")
        self.page._add_exp()
        self.page._add_skill("Python")
        snap = self.page._snapshot()
        self.assertEqual(snap["cards"]["exp"], 2)
        self.assertIn("Python", snap["skills"])
        self.assertEqual(self._exp_card_count(), 2)
        self.assertTrue(all(k.strip() for k in snap["values"]),
                        "快照不该出现空键（会把值贴到错误的字段上）")

    def test_apply_snapshot_roundtrip_including_widget_tree(self):
        self.page.name_edit.setText("张三")
        self.page._add_exp()
        self.page._add_skill("Python")
        snap = self.page._snapshot()

        # 破坏现场：改名、清技能、删掉第二张经历卡并**真正脱离控件树**
        self.page.name_edit.setText("李四")
        self.page._skills = []
        dropped = self.page._exp_cards[-1]
        self.page._remove_exp(dropped)
        self.page.exp_layout.removeWidget(dropped)
        dropped.setParent(None)
        self.assertEqual(self._exp_card_count(), 1, "破坏后应只余 1 张卡片")

        self.assertIs(self.page._apply_snapshot(snap), True, "贴回应成功")
        self.assertEqual(self.page.name_edit.text(), "张三")
        self.assertEqual(len(self.page._exp_cards), 2)
        self.assertIn("Python", self.page._skills)
        self.assertEqual(self._exp_card_count(), 2,
                         "删掉的卡片必须真的回到控件树里")
        self.assertEqual(self.page._snapshot(), snap, "往返后快照应逐键一致")

    def test_reverse_structure_mismatch_is_refused(self):
        """结构对不上时**必须拒绝**，宁可用户重填，也不能把值贴错字段。"""
        snap = self.page._snapshot()
        broken = dict(snap)
        broken["keys"] = list(snap["keys"]) + ["QLineEdit:不存在的字段#9"]
        self.page.name_edit.setText("原值")
        self.assertIs(self.page._apply_snapshot(broken), False)
        self.assertEqual(self.page.name_edit.text(), "原值", "拒绝时不能动任何字段")

    def test_reverse_delete_card_leaves_widget_tree(self):
        """只 `deleteLater()` 的话卡片还在树里 —— 这条判据必须看得出差别。"""
        self.page._add_exp()
        card = self.page._exp_cards[-1]
        self.page._remove_exp(card)
        card.deleteLater()
        self.assertEqual(self._exp_card_count(), 2,
                         "deleteLater 只是排队，控件仍在树里（判据要认得这个差别）")
        card.setParent(None)
        self.assertEqual(self._exp_card_count(), 1)

    def test_undo_redo_through_page_entry_points(self):
        self.page.name_edit.setText("张三")            # 先有个「改之前」的样子
        self.page._undo.reset(self.page._snapshot())
        self.page.name_edit.setText("王五")
        self.page._on_form_settled()          # 等价于输入停顿后落一次快照
        self.assertTrue(self.page._undo.can_undo)
        self.page._undo_step()
        self.assertEqual(self.page.name_edit.text(), "张三")
        self.page._redo_step()
        self.assertEqual(self.page.name_edit.text(), "王五")

    def test_settled_is_deduped(self):
        """连续按键不该把栈吃光。"""
        self.page._undo.reset(self.page._snapshot())
        for _ in range(5):
            self.page._on_form_settled()
        self.assertEqual(self.page._undo.undo_depth, 0,
                         "没有变化时落快照不该进栈")

    def test_shortcuts_do_not_hijack_ctrl_z(self):
        """Ctrl+Z 是输入框自带的逐字段撤销，抢过来比没有更糟。"""
        keys = {s.key().toString() for s in self.page.findChildren(
            __import__("PySide6.QtGui", fromlist=["QShortcut"]).QShortcut)}
        self.assertNotIn("Ctrl+Z", keys)
        self.assertNotIn("Ctrl+Y", keys)
        self.assertIn("Ctrl+Alt+Z", keys)

    def test_draft_is_saved_and_restored(self):
        self.page.name_edit.setText("草稿名字")
        self.page._on_form_settled()
        row = database.load_draft("resume_generate")
        self.assertIsNotNone(row, "落定时应写一份本地草稿")
        self.assertIn("草稿名字", list(row["payload"]["values"].values()))

    def test_incompatible_draft_is_refused_not_smeared(self):
        """草稿结构与当前表单对不上（版本更新改过表单）时：

        必须**拒绝恢复并如实说明**，而不是把值按顺序贴到别的字段上 ——
        后者比丢掉草稿糟得多（用户会以为自己的内容被改了）。
        """
        from offerflow_desktop.ui.pages import resume_generate as rg
        database.save_draft("resume_generate",
                            {"keys": ["QEdit:上古字段#0"],
                             "values": {"QEdit:上古字段#0": "旧版本的值"},
                             "cards": {"exp": 0}, "skills": []})
        box = mock.MagicMock()
        box.question.return_value = box.StandardButton.Yes
        with mock.patch.object(rg, "QMessageBox", box):
            self.page._maybe_restore_draft()          # 不能抛
        titles = [c[0][1] for c in box.information.call_args_list]
        self.assertIn("无法恢复", titles, "拒绝恢复必须让用户知道")
        self.assertEqual(self.page.name_edit.text(), "", "拒绝时不能动任何字段")
        self.assertIsNone(database.load_draft("resume_generate"),
                          "拒绝过的草稿要清掉，否则每次进页面都再问一遍")

    def test_empty_draft_is_dropped_silently(self):
        """上次只是打开了页面没填东西 —— 不值得弹框打扰。"""
        from offerflow_desktop.ui.pages import resume_generate as rg
        database.save_draft("resume_generate",
                            {"keys": ["QLineEdit:x#0"], "values": {"QLineEdit:x#0": "  "},
                             "cards": {"exp": 1}, "skills": []})
        box = mock.MagicMock()
        with mock.patch.object(rg, "QMessageBox", box):
            self.page._maybe_restore_draft()
        self.assertFalse(box.question.called, "空草稿不该弹框")
        self.assertIsNone(database.load_draft("resume_generate"))

    def test_restored_draft_resets_undo_baseline(self):
        """恢复完要把撤销起点挪到恢复后的状态，否则按一次撤销会跳回空表单。"""
        from offerflow_desktop.ui.pages import resume_generate as rg
        # 草稿必须是**结构一致**的一份真实快照：先填、再取快照、再清空现场。
        self.page.name_edit.setText("草稿里的名字")
        snap = self.page._snapshot()
        database.save_draft("resume_generate", snap)
        self.page.name_edit.setText("")
        self.assertTrue(snap["values"], "快照里应有非空值，否则会被当成空草稿")

        box = mock.MagicMock()
        box.question.return_value = box.StandardButton.Yes
        with mock.patch.object(rg, "QMessageBox", box):
            self.page._maybe_restore_draft()
        self.assertEqual(self.page.name_edit.text(), "草稿里的名字", "草稿没被贴回")
        self.assertFalse(self.page._undo.can_undo,
                         "恢复后撤销起点应是恢复后的状态")


if __name__ == "__main__":
    unittest.main(verbosity=2)
