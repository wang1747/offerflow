#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""面经「从文件 / 截图 OCR 导入」（桌面版把解析放在本机，原文件不出本机）。

对标的网页版行为
----------------
网页版把原文件 multipart 传给服务端解析；桌面版走 `local_ai.parse`
（PDF/DOCX/TXT/MD + 图片走 Windows 系统 OCR），只把**文字**入库。

本文件钉住的语义（每条都有反向对照）
------------------------------------
1. 识别结果**先填进文本框**，由用户核对后再点「导入面经」——解析完不自动入库。
   反向：识别噪声直接被 `save_article` 写进库 ⇒ 判据变红。
2. 填框是**追加**不是覆盖（用户可能已经粘了一段）。
   反向：`setPlainText(text)` 冲掉用户已粘的内容 ⇒ 判据变红。
3. 没识别出文字要**报错**，不能悄悄把空串填进去。
4. 筛选器里只列 `parse_document` 真支持的扩展名（列了 .html 会让用户选完才吃报错）。
5. 解析失败要提示，且不能把两个入口按钮永久禁用（否则用户再也点不动）。

运行：python tests/test_interview_import.py
"""

import os
import sys
import types
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from PySide6.QtWidgets import QApplication     # noqa: E402

_APP = QApplication.instance() or QApplication([])

from offerflow_desktop import api_client as db                       # noqa: E402
from offerflow_desktop import local_ai                               # noqa: E402
from offerflow_desktop.local_ai import parse as lparse               # noqa: E402
from offerflow_desktop.ui.pages import interview as iv               # noqa: E402


class _FakeMW:
    def refresh_status(self):
        pass

    def set_status(self, *a, **k):
        pass


def _result(text="", engine="plain", note=""):
    class _R:
        pass

    r = _R()
    r.text = text
    r.engine = engine
    r.note = note
    return r


class _Base(unittest.TestCase):

    def setUp(self):
        # 页面构造会拉面经列表：桩掉，避免联网（用完还原，别污染同进程其他测试）
        self._saved_mods = {name: getattr(db, name)
                            for name in ("list_articles", "list_resumes", "list_jds",
                                         "save_article")}
        self.addCleanup(self._restore_mods)
        db.list_articles = lambda: []
        db.list_resumes = lambda: []
        db.list_jds = lambda: []
        self.saved = []
        db.save_article = lambda *a, **k: (self.saved.append((a, k)), {})[1]
        # 离线环境下 QMessageBox.* 是**模态** exec()，一旦弹出就永久阻塞
        # （headless 没人点）。整个测试类统一换成不阻塞的 mock。
        self.qm = mock.MagicMock()
        patcher = mock.patch.object(iv, "QMessageBox", self.qm)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.page = iv.InterviewPage(_FakeMW())
        self.addCleanup(self.page.setParent, None)

    def _restore_mods(self):
        for name, fn in self._saved_mods.items():
            setattr(db, name, fn)

    def _hint(self):
        return self.page.art_extract_hint.text()


class TestExtractFillsBox(_Base):

    def test_extract_fills_box_and_does_not_save(self):
        """识别结果只进输入框 —— 绝不自动入库。"""
        self.page._on_extracted("x.pdf", _result(text="面试官问了 HTTP 缓存", engine="liteparse"))
        self.assertIn("HTTP 缓存", self.page.art_content.toPlainText())
        self.assertEqual(self.saved, [], "解析完不该调用 save_article（要等用户确认）")

    def test_extract_appends_instead_of_overwriting(self):
        self.page.art_content.setPlainText("我先前粘的一段")
        self.page._on_extracted("x.pdf", _result(text="新识别的一段"))
        text = self.page.art_content.toPlainText()
        self.assertIn("我先前粘的一段", text, "不能把用户已粘的内容冲掉")
        self.assertIn("新识别的一段", text)

    def test_ocr_note_is_surfaced(self):
        self.page._on_extracted("s.png", _result(
            text="三道题", engine="ocr", note="图片经系统 OCR 识别（可能有错字，请核对）"))
        self.assertIn("OCR", self._hint())
        self.assertIn("核对", self._hint(), "OCR 场景必须提醒核对")

    def test_hint_asks_for_company_when_empty(self):
        self.page.art_company.setText("")
        self.page._on_extracted("x.pdf", _result(text="abc"))
        self.assertIn("公司", self._hint())
        self.page.art_company.setText("字节跳动")
        self.page._on_extracted("x.pdf", _result(text="abc"))
        self.assertNotIn("请补填公司名", self._hint())

    def test_empty_text_reports_instead_of_filling_blank(self):
        self.page._on_extracted("x.pdf", _result(text="   ", note="本机未启用 Windows OCR 引擎"))
        self.assertTrue(self.qm.warning.called, "识别不出文字必须提示")
        self.assertEqual(self.page.art_content.toPlainText(), "", "不该填空串")
        self.assertIn("OCR", self.qm.warning.call_args[0][2], "要把引擎那句话透给用户")

    def test_failed_extract_reports_and_reenables_buttons(self):
        self.page.art_file_btn.setEnabled(False)
        self.page.art_ocr_btn.setEnabled(False)
        self.page._on_extract_failed("文件不存在：x.pdf")
        self.assertTrue(self.qm.warning.called)
        self.assertTrue(self.page.art_file_btn.isEnabled(), "失败后按钮必须恢复可点")
        self.assertTrue(self.page.art_ocr_btn.isEnabled())


class _Sig:
    """极简信号替身（connect / disconnect / emit）。"""

    def __init__(self):
        self.slots = []

    def connect(self, fn):
        self.slots.append(fn)

    def disconnect(self):
        self.slots = []

    def emit(self, *args):
        for fn in list(self.slots):
            fn(*args)


class _StubWorker:
    """假 worker：只提供信号，用来验证页面销毁后的断连行为。"""

    def __init__(self, *args, **kwargs):
        self.success = _Sig()
        self.failed = _Sig()

    def start(self):
        pass


class TestWorkerLifecycle(_Base):
    """#4：页面销毁必须断开后台回调（否则回调摸已释放的 Qt 对象 → 静默崩退）。"""

    def test_destroyed_signal_triggers_cleanup(self):
        stub = _StubWorker()
        self.page._workers.append(stub)
        self.page.destroyed.emit()          # 模拟子页面析构
        self.assertEqual(self.page._workers, [], "destroyed 没有接到清理钩子")

    def test_cleanup_disconnects_both_signals(self):
        stub = _StubWorker()
        seen = []
        stub.success.connect(lambda r: seen.append(("ok", r)))
        stub.failed.connect(lambda m: seen.append(("err", m)))
        self.page._workers.append(stub)
        self.page._cleanup_workers()
        stub.success.emit({"x": 1})
        stub.failed.emit("boom")
        self.assertEqual(seen, [], "断连后仍触发了回调")

    def test_start_simulate_registers_its_worker(self):
        """真起会话时那个 worker 必须被登记，否则销毁时断不掉它。"""
        self.page.sim_resume_combo.addItem("简历A", "r-1")
        self.page.sim_jd_combo.addItem("岗位A", "jd-1")
        with mock.patch.object(iv, "LLMWorker", _StubWorker):
            self.page._start_simulate()
        self.assertEqual(len(self.page._workers), 1, "会话 worker 没有登记")


class TestSimulateButtonState(_Base):
    """#2：面试进行中不允许再点「开始模拟面试」（旧版会串台）。

    旧版 `_sync_resume_state` 只看「有没有选简历」，而 `_on_first_question` 回调里
    又会调它 → 正在进行的面试中按钮被重新点亮，能再开一场，把 `_questions/_answers`
    就地清零、前一场永不收口。
    """

    def _select_sim_resume(self):
        self.page.sim_resume_combo.addItem("简历A", "r-1")
        self.page._sync_resume_state()

    def _enter_session(self):
        """模拟 `_start_simulate` 已跑过：置「进行中」并接到第一题。"""
        self.page._sim_running = True
        self.page._on_first_question({"session_id": "s1", "question": "请自我介绍"})

    def test_first_question_keeps_start_disabled(self):
        self._select_sim_resume()
        self.assertTrue(self.page.sim_start_btn.isEnabled(), "选了简历后应可开始")
        self._enter_session()
        self.assertFalse(self.page.sim_start_btn.isEnabled(),
                         "面试已开始，开始按钮不该被重新点亮")

    def test_summary_releases_and_reenables(self):
        self._select_sim_resume()
        self._enter_session()
        self.assertFalse(self.page.sim_start_btn.isEnabled())
        self.page._on_summary({"summary": "整场总结"})
        self.assertFalse(self.page._sim_running)
        self.assertTrue(self.page.sim_start_btn.isEnabled(),
                        "一场结束后应能直接再开一场，无需切页再回来")

    def test_start_failure_releases_running_flag(self):
        """起会话失败必须解除「进行中」，否则开始按钮被永久卡死。"""
        self._select_sim_resume()
        self.page._sim_running = True
        self.page._on_simulate_start_failed("网络错误")
        self.assertFalse(self.page._sim_running)
        self.assertTrue(self.page.sim_start_btn.isEnabled())
        self.assertTrue(self.qm.warning.called, "失败要提示用户")


class TestPathPicker(_Base):

    def test_picking_a_file_runs_local_parse(self):
        calls = []
        with mock.patch.object(iv, "QFileDialog") as fd, \
                mock.patch.object(local_ai, "parse_document",
                                  lambda p: (calls.append(p), _result(text="正文"))[1]):
            fd.getOpenFileName.return_value = ("D:/a/b.pdf", "")
            self.page._import_from_path(is_image=False)
            self._pump()
        self.assertEqual(calls, ["D:/a/b.pdf"], "应在本机调用 parse_document")
        self.assertIn("正文", self.page.art_content.toPlainText())

    def test_image_filter_used_for_screenshots(self):
        with mock.patch.object(iv, "QFileDialog") as fd:
            fd.getOpenFileName.return_value = ("", "")
            self.page._import_from_path(is_image=True)
            used = fd.getOpenFileName.call_args[0][3]
        self.assertEqual(used, iv.IMG_FILTER, "截图走图片筛选器")

    def test_cancel_does_not_start_worker(self):
        with mock.patch.object(iv, "QFileDialog") as fd:
            fd.getOpenFileName.return_value = ("", "")
            self.page._import_from_path(is_image=False)
        self.assertIsNone(self.page._extract_worker, "取消选择不该起解析线程")

    def test_parse_failure_is_reported_not_swallowed(self):
        with mock.patch.object(iv, "QFileDialog") as fd, \
                mock.patch.object(local_ai, "parse_document",
                                  side_effect=ValueError("不支持的文件类型：.html")):
            fd.getOpenFileName.return_value = ("D:/a/b.html", "")
            self.page._import_from_path(is_image=False)
            self._pump()
        self.assertTrue(self.qm.warning.called, "解析失败要提示")
        self.assertIn("不支持的文件类型",
                      str(self.qm.warning.call_args[0][2]))

    def test_filters_only_contain_parseable_exts(self):
        """筛选器里的扩展名必须是 parse_document 真支持的。

        反向：把 .html 列进去 —— 用户选完才吃「不支持的文件类型」，
        这是最没必要的失败（网页版支持 html，桌面版本地解析器不支持）。
        """
        import re
        supported = set(lparse.SUPPORTED_EXT)
        for flt in (iv.DOC_FILTER, iv.IMG_FILTER):
            exts = set(re.findall(r"\*(\.[a-z0-9]+)", flt.split(";;")[0]))
            self.assertTrue(exts, "筛选器里应至少有一个扩展名")
            self.assertTrue(exts <= supported,
                            f"{exts - supported} 不在 local_ai.parse 支持列表里")
        self.assertNotIn(".html", iv.DOC_FILTER,
                         "本地解析器不支持 html，别列进筛选器")

    def _pump(self):
        from offerflow_desktop.llm import _ALIVE
        for _ in range(200):
            _APP.processEvents()
            if not _ALIVE:
                break


class TestImportStillWorks(_Base):

    def test_manual_import_still_goes_through_save_article(self):
        """原有「粘贴 → 导入面经」链路不能被新加的两个入口弄坏。"""
        self.page.art_company.setText("字节跳动")
        self.page.art_position.setText("后端")
        self.page.art_content.setPlainText("面试流程：一面算法、二面项目")
        self.page._import_article()
        self.assertEqual(len(self.saved), 1, "手填内容仍要正常入库")
        args, _kw = self.saved[0]
        self.assertEqual(args[0], "字节跳动")
        self.assertIn("一面算法", args[1])

    def test_save_article_signature_accepts_text_import(self):
        """本机解析出的文字走的是同一张表、同一个接口（raw_text 入库）。"""
        import inspect
        real = self._saved_mods["save_article"]      # setUp 里桩过，用原件查签名
        params = list(inspect.signature(real).parameters)
        self.assertEqual(params[:3], ["company", "raw_content", "position"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
