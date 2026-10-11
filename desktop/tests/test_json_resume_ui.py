"""JSON Resume 的 UI 接线判据：导入对话框、导出按钮、导出反馈文案。

跑法：python tests/test_json_resume_ui.py

与 `test_tracker_calendar_ui.py` 同一套装置：offscreen 平台 + 桩主窗口 + mock
文件对话框。**不 show() 任何窗口** —— 本平台下 show 过的窗口在进程退出时会让
Qt 的 C 层析构改掉退出码（错题本 W 类），那会让 run_all 假红。
"""

import json
import os
import sys
import tempfile
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from PySide6.QtWidgets import QApplication, QWidget                 # noqa: E402

from offerflow_desktop.core import json_resume as jr               # noqa: E402
from offerflow_desktop.core.json_resume import JsonVerifyResult      # noqa: E402
from offerflow_desktop.ui import export_result, import_resume        # noqa: E402
from offerflow_desktop.ui.pages import resume_generate               # noqa: E402

_app = QApplication.instance() or QApplication([])

SAMPLE = {
    "basics": {"name": "李四", "label": "RAG 工程师", "email": "lisi@example.com",
               "phone": "13900139000", "summary": "两年检索与文档解析经验。"},
    "work": [{"name": "某某科技", "position": "算法工程师", "startDate": "2024-07",
              "highlights": ["把召回率提了 12%"]}],
    "education": [{"institution": "某某大学", "area": "计算机科学与技术",
                   "studyType": "硕士", "startDate": "2021-09", "endDate": "2024-06"}],
    "skills": [{"name": "编程语言", "keywords": ["Python", "Go"]}],
    "certificates": [{"name": "CET-6"}],
}

STRUCTURED = {
    "name": "李四", "position": "RAG 工程师",
    "contact": {"phone": "13900139000", "email": "lisi@example.com"},
    "summary": "两年检索与文档解析经验。",
    "education": [{"school": "某某大学", "major": "计算机科学与技术", "degree": "硕士",
                   "start": "2021.09", "end": "2024.06", "detail": "GPA 3.9"}],
    "experiences": [{"name": "某某科技", "role": "算法工程师", "type": "work",
                     "start": "2024.07", "end": "至今",
                     "bullets": ["把召回率提了 12%"]}],
    "skills": [], "skill_groups": {"编程语言": ["Python", "Go"]},
    "certifications": ["CET-6"],
}


class _FakeMainWindow(QWidget):
    """功能页构造时用到的宿主接口（与 tests/test_pages_smoke.py 同一套）。"""

    def refresh_status(self):
        pass

    def refresh_ai_source(self):
        pass

    def navigate(self, *args, **kwargs):
        pass

    def set_status(self, *args, **kwargs):
        pass

    def apply_theme(self, *args, **kwargs):
        pass


class _TmpMixin(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="test_jr_ui_")

    def write(self, name, content: str) -> str:
        path = os.path.join(self.tmp, name)
        with open(path, "wb") as fh:
            fh.write(content.encode("utf-8"))
        return path

    def write_json(self, name, payload) -> str:
        return self.write(name, json.dumps(payload, ensure_ascii=False))


class ExtractAnyTest(_TmpMixin):

    def test_reads_json_resume_into_plain_text(self):
        path = self.write_json("ok.json", SAMPLE)
        text, note = import_resume.extract_any(path)
        self.assertIn("李四", text)
        self.assertIn("某某科技", text)
        self.assertIn("RAG 工程师", text)      # 求职意向被渲染进正文
        self.assertIn("- 把召回率提了 12%", text)
        self.assertEqual(note, "")

    def test_accepts_utf8_bom(self):
        path = self.write("bom.json", "\ufeff" + json.dumps(SAMPLE, ensure_ascii=False))
        text, _ = import_resume.extract_any(path)
        self.assertIn("李四", text)

    def test_bad_files_raise_readable_value_errors(self):
        cases = (("half.json", "{", "JSON"),
                 ("alien.json", '{"foo": 1}', "JSON Resume"),
                 ("array.json", "[1, 2]", "对象"),
                 ("empty.json", "", "空"))
        for name, content, needle in cases:
            with self.subTest(name=name):
                path = self.write(name, content)
                with self.assertRaises(ValueError) as ctx:
                    import_resume.extract_any(path)
                self.assertIn(needle, str(ctx.exception))

    def test_non_utf8_json_is_rejected_with_hint(self):
        path = os.path.join(self.tmp, "gbk.json")
        with open(path, "wb") as fh:
            fh.write(json.dumps(SAMPLE, ensure_ascii=False).encode("gbk"))
        with self.assertRaises(ValueError) as ctx:
            import_resume.extract_any(path)
        self.assertIn("UTF-8", str(ctx.exception))


class ImportDialogTest(_TmpMixin):

    def test_picking_json_fills_preview_and_payload(self):
        path = self.write_json("ok.json", SAMPLE)
        dialog = import_resume.ImportResumeDialog()
        with mock.patch.object(import_resume.QFileDialog, "getOpenFileName",
                               return_value=(path, "")):
            dialog._pick_file()
        self.assertIn("李四", dialog.preview.toPlainText())
        self.assertIn("ok.json", dialog.file_label.text())
        self.assertTrue(dialog.ok_btn.isEnabled())
        dialog._import()
        self.assertIsNotNone(dialog.payload)
        self.assertTrue(dialog.payload["raw_text"].startswith("李四"))
        self.assertEqual(dialog.payload["source_file"], "ok.json")

    def test_unrelated_json_is_blocked_before_upload(self):
        """无关 JSON 必须在选文件那一步就被挡下，而不是一路走到上传才失败。"""
        path = self.write_json("alien.json", {"foo": 1, "bar": 2})
        dialog = import_resume.ImportResumeDialog()
        warnings = []
        with mock.patch.object(import_resume.QFileDialog, "getOpenFileName",
                               return_value=(path, "")), \
                mock.patch.object(import_resume.QMessageBox, "warning",
                                  side_effect=lambda *a, **k: warnings.append(a[2])):
            dialog._pick_file()
        self.assertTrue(warnings)
        self.assertIn("JSON Resume", warnings[0])
        self.assertEqual(dialog.preview.toPlainText(), "")
        self.assertFalse(dialog.ok_btn.isEnabled())


class ExportJsonTest(unittest.TestCase):
    """真构造页面 → 落盘 → 从磁盘读回来自检 → 反馈带 kind。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="test_jr_export_")
        self.page = resume_generate.ResumeGeneratePage(_FakeMainWindow())
        self.page._current_result = {
            "resume_text": "李四\n13900139000 | lisi@example.com\n",
            "structured": STRUCTURED,
        }
        self.target = os.path.join(self.tmp, "out.json")
        self.calls = []

    def _export(self):
        with mock.patch.object(resume_generate.QFileDialog, "getSaveFileName",
                               return_value=(self.target, "")), \
                mock.patch.object(resume_generate, "notify_export_result",
                                  side_effect=lambda *a, **k: self.calls.append((a, k))), \
                mock.patch.object(resume_generate, "confirm_placeholders",
                                  return_value=True):
            self.page._export("json")

    def test_writes_a_valid_json_resume(self):
        self._export()
        self.assertTrue(os.path.isfile(self.target))
        with open(self.target, encoding="utf-8") as fh:
            data = json.load(fh)
        self.assertEqual(data["basics"]["name"], "李四")
        self.assertEqual([w["name"] for w in data["work"]], ["某某科技"])
        self.assertNotIn("endDate", data["work"][0])     # 「至今」不能写成 endDate

    def test_notification_carries_kind_and_verify_result(self):
        self._export()
        self.assertEqual(len(self.calls), 1)
        args, kwargs = self.calls[0]
        self.assertEqual(kwargs.get("kind"), "json")
        verify = args[3]
        self.assertIsInstance(verify, JsonVerifyResult)
        self.assertTrue(verify.ok, verify.problems)

    def test_written_bytes_are_exactly_what_we_encoded(self):
        """导出必须走字节写入：文本模式会把 \\n 翻成 \\r\\n，与内存里的字符串不一致。"""
        self._export()
        with open(self.target, "rb") as fh:
            raw = fh.read()
        self.assertNotIn(b"\r\n", raw)
        self.assertEqual(raw, jr.dumps(STRUCTURED).encode("utf-8"))


class ByteWriteControlTest(unittest.TestCase):
    """反向对照：证明「必须写字节」不是一句空话。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="test_jr_bytes_")
        self.payload = jr.dumps(STRUCTURED)

    def test_text_mode_would_introduce_crlf(self):
        path = os.path.join(self.tmp, "text.json")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(self.payload)
        with open(path, "rb") as fh:
            raw = fh.read()
        self.assertIn(b"\r\n", raw, "文本模式本该产生 CRLF —— 判据没抓到任何东西")
        self.assertNotEqual(raw, self.payload.encode("utf-8"))

    def test_byte_mode_matches_the_string(self):
        path = os.path.join(self.tmp, "byte.json")
        with open(path, "wb") as fh:
            fh.write(self.payload.encode("utf-8"))
        with open(path, "rb") as fh:
            raw = fh.read()
        self.assertNotIn(b"\r\n", raw)
        self.assertEqual(raw, self.payload.encode("utf-8"))


class FeedbackKindTest(unittest.TestCase):
    """同一份坏结果，PDF 与 JSON 给出的下一步必须不同。

    （这条实际测的是 `ui/export_result.py` 的 kind 分支 —— 那个参数就是为 JSON
    导出加的，所以放在这里一起守。）
    """

    def setUp(self):
        self.bad = JsonVerifyResult(ok=False, problems=["顶层出现了不认识的字段：foo"])

    def test_both_are_warnings(self):
        self.assertEqual(export_result.export_message("x", "", self.bad, "pdf")[0], "warn")
        self.assertEqual(export_result.export_message("x", "", self.bad, "json")[0], "warn")

    def test_json_advice_is_about_other_platforms(self):
        body = export_result.export_message("x", "", self.bad, "json")[2]
        self.assertIn("平台", body)
        self.assertNotIn("招聘系统", body)
        self.assertNotIn("再投", body)

    def test_pdf_advice_unchanged(self):
        body = export_result.export_message("x", "", self.bad, "pdf")[2]
        self.assertIn("招聘系统", body)
        self.assertIn("再投", body)

    def test_default_kind_is_still_pdf(self):
        """旧调用点不传 kind，行为必须一点不变。"""
        self.assertEqual(export_result.export_message("x", "", self.bad)[2],
                         export_result.export_message("x", "", self.bad, "pdf")[2])

    def test_unknown_kind_falls_back_to_pdf_advice(self):
        self.assertEqual(export_result.export_message("x", "", self.bad, "docx")[2],
                         export_result.export_message("x", "", self.bad, "pdf")[2])

    def test_passing_check_is_info(self):
        level, _, body = export_result.export_message("x", "", JsonVerifyResult(ok=True),
                                                       "json")
        self.assertEqual(level, "info")
        self.assertIn("合法", body)

    def test_no_result_is_info_without_inventing_a_conclusion(self):
        level, _, body = export_result.export_message("x", "", None, "json")
        self.assertEqual(level, "info")
        self.assertNotIn("自检", body)


if __name__ == "__main__":
    unittest.main(verbosity=2)
