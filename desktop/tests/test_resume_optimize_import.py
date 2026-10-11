#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""简历优化页「就地建档入口」的判据（G7）。

背景：优化页此前的「简历」下拉在没有简历时**只写了句 tooltip** —— 用户看不到
任何可点的东西，也不知道该去哪儿。而「没有简历就地补一份」这件事，项目里
早有共享实现 `ui/import_resume.py`（本机解析 Word/PDF、只上行抽出来的文字），
面试准备 / 岗位匹配 / 薪资谈判三页都接了，**唯独优化页没接**。

这次补齐两条路（对应两类用户）：

  * 「导入简历」——手上有简历、想针对岗位改一版；
  * 「去生成简历」——手上根本没有简历（网页版这里就是一个跳去生成页的链接）。

判据分三层：

  1. **空态引导**：缺简历时引导条可见 + 主按钮禁用（而不是「点了才弹错」）；
     有简历时引导条收起 + 按钮可用。**必须双向断言** —— 只写前者的话，
     把 `setVisible` 删掉（永远显示）照样绿；

     这一层的「有简历」一半**必须过了 `showEvent` 再断言**：本页构造期只搭界面
     （`resume_hint` 显形 + `optimize_btn` 置灰），下拉是按 `showEvent` 里那次
     `_refresh()` 才填上库里的数据的 —— 构造期就断言「引导条已收起」等于在要求
     把网络请求挪回构造期，那正是 `test_ctor_async_audit.py` 拦的崩溃隐患。
     另一半（构造期的静态空态）由 `test_hint_shows_and_button_disabled_when_no_resume`
     直接覆盖；
  2. **入口接线**：两个按钮真的接上了正确的东西（navigate 的目标页、
     共享导入流程 + 「导入后选中」回调）。这类缺陷是静默的：按钮看着在，
     点了没反应或点错地方；
  3. **导入后选中**：新简历入库后下拉要选中它，而不是停在第一项
     （网页端同一个坑）。另附一条：`_refresh()` 重填下拉**保留当前选择**。

反向对照见 `_mutate_g7_check.py`。

运行：python tests/test_resume_optimize_import.py
"""

import os
import sys
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from PySide6.QtWidgets import QApplication, QWidget                        # noqa: E402

_APP = QApplication.instance() or QApplication([])

from offerflow_desktop.ui import import_resume as ir                        # noqa: E402
from offerflow_desktop.ui import tokens                                     # noqa: E402
from offerflow_desktop.ui.pages import resume_optimize as ro                # noqa: E402

RESUME_1 = {"id": "r1", "raw_text": "张三\nPython 后端\n教育：某大学",
            "parsed_json": {"position": "Python 后端"}, "version": 1}
RESUME_2 = {"id": "r2", "raw_text": "李四\nRAG 算法\n教育：某大学",
            "parsed_json": {"position": "RAG 算法"}, "version": 1}
JDS = [{"id": "j1", "company": "合合信息", "position": "RAG 算法",
        "raw_text": "要求 Python"}]

OPTIMIZED = {"optimized_text": "李四\nRAG 算法（重写后）", "edit_count": 3,
             "gap": {"matched": ["Python"], "partial": [], "missing": []},
             "preservation": {"passed": True, "score": 0.92},
             "changes": []}


class _FakeMW(QWidget):
    """记录 navigate 目标，供「按钮跳到哪一页」的判据断言。"""

    def __init__(self):
        super().__init__()
        self.navigated = []

    def refresh_status(self):
        pass

    def navigate(self, key, *a, **k):
        self.navigated.append(key)
        return True

    def open_agent(self, *a, **k):
        pass


class _Base(unittest.TestCase):

    def setUp(self):
        self.addCleanup(tokens.set_theme, "light")
        self.addCleanup(_APP.setStyleSheet, "")
        # 简历列表做成**可变**的：用例可以先构造出「空库」的页面，
        # 再模拟「导入成功、库里多了一份」的真实时序。
        self._resume_rows = [dict(RESUME_1)]
        p = mock.patch.object(ro.db, "list_resumes",
                              side_effect=lambda: [dict(r) for r in self._resume_rows])
        p.start()
        self.addCleanup(p.stop)
        p = mock.patch.object(ro.db, "list_jds", return_value=[dict(JDS[0])])
        p.start()
        self.addCleanup(p.stop)
        # 版本列表走后台线程（LLMWorker.start()）。用例不验证版本加载，且必须
        # hermetic：桩成空列表，避免 worker 触发真实 I/O / 退出期线程崩溃
        # （G5 同类的「构造/交互触发真实线程链」问题）。
        p = mock.patch.object(ro.db, "list_resume_versions", return_value=[])
        p.start()
        self.addCleanup(p.stop)
        # headless 下 QMessageBox.* 是模态 exec()，会永久阻塞
        self.qm = mock.MagicMock()
        p = mock.patch.object(ro, "QMessageBox", self.qm)
        p.start()
        self.addCleanup(p.stop)
        self.mw = _FakeMW()
        self.addCleanup(self.mw.deleteLater)

    def _page(self, resumes=None):
        if resumes is not None:
            self._resume_rows = [dict(r) for r in resumes]
        page = ro.ResumeOptimizePage(self.mw)
        self.addCleanup(page.setParent, None)
        self.addCleanup(page.deleteLater)
        return page

    def _shown(self, resumes=None):
        """把页面推进到「已显示」状态：两个理由。

        * 点了才发信号的控件（QPushButton）必须先 show 再 click；
        * 下拉是 `showEvent` 里 `QTimer.singleShot(0, self._refresh)` 填的，
          所以「有没有简历」这类**数据态**要 `show()` + 跑一轮事件循环之后才成立
          （构造期只有静态初始态）。
        """
        page = self._page(resumes)
        page.show()
        _APP.processEvents()
        self.addCleanup(page.hide)
        return page


# ============================================================
# 1. 空态引导（双向）
# ============================================================

class TestEmptyGuidance(_Base):

    def test_hint_shows_and_button_disabled_when_no_resume(self):
        page = self._page([])
        self.assertFalse(page.resume_hint.isHidden(),
                         "没有简历时引导条是收着的 —— 用户看不到任何可点的路")
        self.assertFalse(page.optimize_btn.isEnabled(),
                         "没有简历还能点「开始优化」→ 点了才弹错，属于把问题推给用户")

    def test_hint_hides_and_button_enabled_when_resume_exists(self):
        # 数据态在 show 之后（下拉由 showEvent 里的 _refresh 填），见文件头说明
        page = self._shown([RESUME_1])
        self.assertTrue(page.resume_hint.isHidden(),
                        "有简历了还挂着「还没有简历」的引导条")
        self.assertTrue(page.optimize_btn.isEnabled())

    def test_hint_tracks_the_data_both_ways(self):
        """同一条判据走完整链路：空 → 有 → 空。"""
        page = self._page([])
        self.assertFalse(page.resume_hint.isHidden())

        self._resume_rows = [dict(RESUME_1)]
        page._refresh()
        self.assertTrue(page.resume_hint.isHidden())
        self.assertTrue(page.optimize_btn.isEnabled())

        self._resume_rows = []
        page._refresh()
        self.assertFalse(page.resume_hint.isHidden())
        self.assertFalse(page.optimize_btn.isEnabled())

    def test_reverse_guard_still_exists_when_the_click_gets_through(self):
        """兜底守卫不能删：万一按钮被程序化点开，也要给提示而不是崩。"""
        page = self._page([])
        page._optimize()
        self.qm.warning.assert_called_once()


# ============================================================
# 2. 两个建档入口的接线
# ============================================================

class TestEntryWiring(_Base):

    def test_generate_button_navigates_to_resume_generate(self):
        page = self._shown([])
        page.gen_resume_btn.click()
        self.assertEqual(self.mw.navigated, ["简历生成"],
                         "「去生成简历」没跳到简历生成页（或跳错了页）")

    def test_generate_button_is_a_ghost_so_it_does_not_outrank_start(self):
        page = self._page([])
        self.assertEqual(page.gen_resume_btn.property("class"), "ghost",
                         "次要入口用主按钮样式 → 与「开始优化」抢视觉重心")

    def test_import_button_runs_the_shared_flow_with_the_right_callback(self):
        """「导入简历」必须是**共享流程 + 本页回调**，不能自己另起一套。

        判据要落在「接线」上：按钮在不在，跟点下去有没有走对流程是两件事。
        回调接错（比如接成 no-op）时按钮照样能点、对话框照样弹，
        但导入完成后下拉不会选中新简历 —— 只有这条能抓住。
        """
        page = self._shown([])
        with mock.patch.object(ir, "import_resume_and_select") as fake:
            page.import_btn.click()
        self.assertEqual(fake.call_count, 1, "「导入简历」点了没走共享导入流程")
        args, kwargs = fake.call_args
        self.assertIs(args[0], page, "共享流程收到的宿主不是本页（回调会失效）")
        self.assertEqual(args[1], page._on_resume_imported)

    def test_import_button_exists_even_when_a_resume_is_selected(self):
        """导入不是空态专属：手上这份不满意、换一版是常见路径。"""
        page = self._page([RESUME_1])
        self.assertTrue(page.import_btn.isVisibleTo(page),
                        "有简历时把「导入简历」藏了 —— 想换一版的人只能先删掉旧的")


# ============================================================
# 3. 导入后选中 + 重填保留选择
# ============================================================

class TestSelectionAfterImport(_Base):

    def test_imported_resume_gets_selected(self):
        page = self._shown([RESUME_1, RESUME_2])
        self.assertEqual(page.resume_combo.currentData(), "r1")
        page._on_resume_imported("r2")
        self.assertEqual(page.resume_combo.currentData(), "r2",
                         "导入完没选中新简历 —— 用户以为导入失败，或者改错了简历")

    def test_import_into_empty_library_clears_the_guidance(self):
        """空库导入成功后，引导条要收起、主按钮要解锁（同一个进口的收尾）。"""
        page = self._page([])
        self.assertFalse(page.resume_hint.isHidden())

        self._resume_rows = [dict(RESUME_2)]
        page._on_resume_imported("r2")

        self.assertTrue(page.resume_hint.isHidden())
        self.assertTrue(page.optimize_btn.isEnabled())
        self.assertEqual(page.resume_combo.currentData(), "r2")

    def test_refresh_keeps_the_current_selection(self):
        """重填下拉不能把选择打回第一项。

        原先 `_refresh()` 一律 `clear()` 再重填，index 归零 —— 回滚版本后
        刷新会把「正在改的那份简历」悄悄换成列表第一份，后续操作全跑偏。
        """
        page = self._shown([RESUME_1, RESUME_2])
        self.assertEqual(page.resume_combo.count(), 2)   # 前置：下拉真的填上了
        # 用 blockSignals 设定初始选择，避免触发 currentIndexChanged → _load_versions
        # 起后台线程（用例只验证「_refresh 保留选择」，版本加载是无关副作用）。
        page.resume_combo.blockSignals(True)
        page.resume_combo.setCurrentIndex(1)
        page.resume_combo.blockSignals(False)
        self.assertEqual(page.resume_combo.currentData(), "r2")

        page._refresh()
        self.assertEqual(page.resume_combo.currentData(), "r2",
                         "重填下拉把当前选择弄丢了")

    def test_refresh_with_unknown_id_falls_back_instead_of_crashing(self):
        """`select_resume` 指向一份已不在库里的简历（并发删除）不能崩。"""
        page = self._page([RESUME_1])
        page._refresh(select_resume="不存在")
        self.assertEqual(page.resume_combo.count(), 1)
        self.assertEqual(page.resume_combo.currentData(), "r1")


# ============================================================
# 4. 跑完一轮后的按钮状态
# ============================================================

class TestSyncAfterRun(_Base):

    def test_failed_run_keeps_the_button_disabled_without_a_resume(self):
        """失败恢复走统一入口，别写死 True —— 没简历时不该被「恢复」成可点。"""
        page = self._page([])
        page._on_failed("接口超时")
        self.assertFalse(page.optimize_btn.isEnabled())

    def test_successful_run_re_enables_when_a_resume_is_selected(self):
        # `_on_optimized` 走 `_sync_action_state()`，它按**下拉当前选中**决定按钮
        # 可用性 —— 所以这一页得先过 showEvent 把下拉填上，否则进来时下拉是空的。
        page = self._shown([RESUME_1])
        page._resume = dict(RESUME_1)
        page._jd = dict(JDS[0])
        page._on_optimized(dict(OPTIMIZED))
        self.assertTrue(page.optimize_btn.isEnabled(),
                        "优化跑完按钮停在禁用态 —— 想再改一版得重启页面")


if __name__ == "__main__":
    unittest.main(verbosity=2)
