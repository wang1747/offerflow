#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""导出后「回读自检」的判据（pdf_verify + export_result）。

为什么需要
----------
「文件写成功」与「写出去的内容全不全」是**两个独立结论**。中文字体子集化与
一页调优四段式的失败模式都是**静默丢字/截断** —— 导出这边不报错，离线读代码
也看不出来，唯一可靠的判据是把刚导出的 PDF 读回来对一遍。

判据的四条边界（每条都对应一种会误报的写法）：
  1. **环境故障 ≠ 内容缺陷**：本机没有 PDF 文本引擎时必须 `degraded=True`
     且 `ok=True`，绝不能报成「内容缺失」；
  2. 引擎可用却**一个字都读不出** ⇒ 真缺陷（字形转成图形 / 字体没嵌入，
     招聘系统同样读不到）；
  3. 整串找不到 = `missing`；**前缀在、整串不在 = `truncated`** ——
     两者修法不同，混在一起报会让人改错地方；
  4. 归一只能做**排版层等价变换**（全角标点、空白），不做同义替换，
     否则「找不到」会被自己抹掉。

⚠ 两个接口事实（值 5 分钟的猜测）：`verify_pdf_text` 吃的是**块**
（`resume_review.from_structured` 的产物，键是 `left`/`period`/`contact_line`），
不是 LLM 原始输出；`render_pdf_bytes` 返回 `(bytes, info)` 二元组。

运行：python tests/test_export_verify.py
"""

import os
import sys
import tempfile
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from offerflow_desktop.core import pdf_verify, resume_review         # noqa: E402
from offerflow_desktop.core.pdf_verify import (                      # noqa: E402
    MIN_COVERAGE, PdfVerifyResult, _iter_body_strings, anchors_of,
    normalize_for_match, verify_pdf_text,
)
from offerflow_desktop.ui import export_result as ER                 # noqa: E402

SAMPLE = {
    "name": "张三",
    "position": "AI 应用开发工程师",
    "contact": {"phone": "13800000000", "email": "zhangsan@example.com"},
    "summary": "三年文档智能与 RAG 系统开发经验，主导过检索链路重构。",
    "education": [{"school": "某某大学", "major": "计算机", "degree": "本科",
                   "start": "2019-09", "end": "2023-06"}],
    "experiences": [{"name": "某公司", "role": "算法工程师", "start": "2023-07",
                     "bullets": ["用 LightRAG 搭建多跳检索链路，召回率提升 23%",
                                 "主导简历解析模块，日均处理 10 万份文档"]}],
    "skills": ["Python", "FastAPI", "PostgreSQL", "PySide6"],
}

BLOCKS = resume_review.from_structured(SAMPLE)


def _full_read_text(blocks=None) -> str:
    """「PDF 里该有的字一个不少」时回读到的文本。

    刻意用 `_iter_body_strings`（期望文本的来源）而不是 `anchors_of`：
    锚点只是**抽查**的短串（要点只取前 16 字、不锚 summary/period），
    拿锚点当「完整回读」会永远到不了 100%，那是用例自己造出来的假失败。
    """
    return "\n".join(s for s in _iter_body_strings(blocks or BLOCKS) if s)


def _engine_available() -> bool:
    try:
        from offerflow_desktop.local_ai import parse
        status = parse.parse_status()
    except Exception:                             # noqa: BLE001
        return False
    return bool(status.get("liteparse") or status.get("pypdf"))


class TestNormalize(unittest.TestCase):

    def test_whitespace_and_fullwidth(self):
        self.assertEqual(normalize_for_match("今 天，我"), normalize_for_match("今天,我"))
        self.assertEqual(normalize_for_match("A　B"), normalize_for_match("AB"))

    def test_does_not_synonymize(self):
        """归一不能变成「同义替换」，否则真正的缺失会被自己抹掉。"""
        self.assertNotEqual(normalize_for_match("召回率"), normalize_for_match("准确率"))

    def test_empty_is_safe(self):
        for value in (None, "", 0, [], {}):
            with self.subTest(value=value):
                self.assertIsInstance(normalize_for_match(value), str)


class TestAnchors(unittest.TestCase):

    def test_anchors_cover_the_right_fields(self):
        anchors = anchors_of(BLOCKS)
        joined = "|".join(anchors)
        self.assertIn("张三", joined)
        self.assertIn("AI应用开发工程师", joined)
        self.assertIn("zhangsan@example.com", joined, "邮箱整体参与比对")
        self.assertIn("某某大学", joined)
        # 锚点是**归一后**的串（全角括号已转半角），所以这里也要过一遍归一 ——
        # 直接拿原始全角串去比会假红。
        self.assertIn(normalize_for_match("某公司（算法工程师）"), joined)
        self.assertIn("Python", joined)

    def test_phone_uses_digits_marker(self):
        anchors = anchors_of(BLOCKS)
        self.assertIn("#digits:13800000000", anchors)

    def test_bullets_are_shortened(self):
        """要点只取前 16 字：整句会因为折行永远对不上、报一堆假阳性。"""
        anchors = anchors_of(BLOCKS)
        full = "用 LightRAG 搭建多跳检索链路，召回率提升 23%"
        self.assertNotIn(normalize_for_match(full), anchors)
        self.assertIn(normalize_for_match(full)[:16], anchors)

    def test_dedup_keeps_order(self):
        anchors = anchors_of(BLOCKS)
        self.assertEqual(len(anchors), len(set(anchors)), "锚点应去重")

    def test_garbage_blocks_are_safe(self):
        self.assertEqual(anchors_of({}), [])
        self.assertEqual(anchors_of({"experiences": [None, 1, "x"]}), [])
        self.assertEqual(anchors_of({"education": [None]}), [])


class TestVerifyPdfText(unittest.TestCase):

    def test_engine_missing_is_degraded_not_a_content_defect(self):
        """环境故障不能报成内容缺陷 —— 这条错了会让人去改没坏的地方。"""
        result = verify_pdf_text("随便", BLOCKS, pages=1, engine="")
        self.assertTrue(result.degraded)
        self.assertTrue(result.ok, "引擎不可用不该判内容不合格")
        self.assertFalse(result.checked, "没比过内容就不能说比过了")
        self.assertTrue(result.note)
        self.assertIn("未能回读", result.summary())

    def test_engine_available_but_nothing_read_is_a_defect(self):
        result = verify_pdf_text("", BLOCKS, pages=1, engine="liteparse")
        self.assertFalse(result.degraded)
        self.assertFalse(result.ok, "引擎可用却读不出字 → 必须判缺陷")
        self.assertTrue(result.note)

    def test_complete_read_passes(self):
        result = verify_pdf_text(_full_read_text(), BLOCKS,
                                 pages=1, engine="liteparse")
        self.assertTrue(result.ok, result.summary())
        self.assertEqual(result.missing, [])
        self.assertEqual(result.truncated, [])
        self.assertGreaterEqual(result.coverage, MIN_COVERAGE)
        self.assertIn("完整", result.summary())

    def test_truncated_is_reported_separately_from_missing(self):
        """前缀在、整串不在 = 截断；要和 missing 分开报（修法不一样）。"""
        bullet = normalize_for_match(
            "用 LightRAG 搭建多跳检索链路，召回率提升 23%")[:16]
        self.assertIn(bullet, anchors_of(BLOCKS), "先确认这个锚点确实存在")
        read = bullet[:len(bullet) - 4]           # 留前缀、砍掉尾巴
        self.assertGreaterEqual(len(read), 6, "前缀要够长，否则会落进 missing")

        result = verify_pdf_text(read, BLOCKS, pages=1, engine="liteparse")
        self.assertIn(bullet, result.truncated,
                      "截断没被单独识别：truncated=%r missing=%r"
                      % (result.truncated[:3], result.missing[:3]))

    def test_reverse_thin_output_goes_red(self):
        """反向对照放在**产物侧**：内容只印了联系方式，必须报缺失。"""
        thin_read = "张三 13800000000 zhangsan@example.com"
        result = verify_pdf_text(thin_read, BLOCKS, pages=1, engine="liteparse")
        self.assertFalse(result.ok)
        self.assertGreater(len(result.missing), 0, "抽薄后必须报缺失")
        self.assertLess(result.coverage, MIN_COVERAGE)

    def test_reverse_phone_separately_reported(self):
        """手机号按纯数字比：PDF 里排成 138-0000-0000 也要认出来。"""
        text = _full_read_text().replace("13800000000", "")
        result = verify_pdf_text(text, BLOCKS, pages=1, engine="liteparse")
        self.assertIn("手机号", result.missing)

    def test_reverse_separator_variant_still_matches(self):
        """排版插了分隔符不该被当成缺失 —— 否则用户会去改没坏的地方。"""
        text = _full_read_text().replace("13800000000", "138-0000-0000")
        result = verify_pdf_text(text, BLOCKS, pages=1, engine="liteparse")
        self.assertNotIn("手机号", result.missing)

    def test_details_lists_truncated_before_missing(self):
        result = PdfVerifyResult(ok=False, engine="liteparse",
                                 truncated=["被截断的"], missing=["找不到的"])
        lines = result.details()
        self.assertTrue(lines[0].startswith("疑似被截断"))
        self.assertTrue(any("在 PDF 里找不到" in l for l in lines))


class TestExportMessage(unittest.TestCase):

    def test_none_result_claims_nothing(self):
        level, _title, body = ER.export_message("C:/x.pdf", "", None)
        self.assertEqual(level, "info")
        self.assertNotIn("自检", body, "没校验就不能暗示校验过了")
        self.assertNotIn("完整", body)

    def test_degraded_says_not_checked(self):
        result = PdfVerifyResult(degraded=True, note="本机没有可用的 PDF 文本解析引擎")
        level, _title, body = ER.export_message("C:/x.pdf", "", result)
        self.assertEqual(level, "info")
        self.assertIn("未做回读校验", body)
        self.assertIn("本机没有可用", body)

    def test_incomplete_is_a_warning_with_actionable_detail(self):
        result = PdfVerifyResult(ok=False, engine="liteparse", pages=1,
                                 read_chars=10, expect_chars=100, coverage=0.1,
                                 missing=["用LightRAG搭建多跳检索链"])
        level, title, body = ER.export_message("C:/x.pdf", "", result)
        self.assertEqual(level, "warn")
        self.assertIn("可能不完整", title)
        self.assertIn("用LightRAG", body, "要给到具体哪一条，否则用户没法改")
        self.assertIn("文件已生成", body, "文件确实在，不能让人以为没导出来")

    def test_ok_reports_summary(self):
        result = PdfVerifyResult(ok=True, engine="liteparse", pages=1,
                                 read_chars=100, expect_chars=100, coverage=1.0)
        level, _title, body = ER.export_message("C:/x.pdf", "", result)
        self.assertEqual(level, "info")
        self.assertIn("回读自检", body)

    def test_notify_pops_warning_only_on_warn(self):
        with mock.patch.object(ER, "QMessageBox") as box:
            bad = PdfVerifyResult(ok=False, engine="liteparse", missing=["x"])
            self.assertEqual(ER.notify_export_result(None, "C:/x.pdf", "", bad), "warn")
            self.assertTrue(box.warning.called)
            box.reset_mock()
            good = PdfVerifyResult(ok=True, engine="liteparse", read_chars=1,
                                   expect_chars=1, coverage=1.0)
            self.assertEqual(ER.notify_export_result(None, "C:/x.pdf", "", good), "info")
            self.assertFalse(box.warning.called, "合格时不该弹警告")


class TestEndToEndAgainstRealPdf(unittest.TestCase):
    """真的导一份 PDF 再回读 —— 唯一能抓住「字体子集化丢字」的路径。"""

    @staticmethod
    def _write(structured, path):
        from offerflow_desktop.services import pdf_exporter
        payload, _info = pdf_exporter.render_pdf_bytes(structured)
        with open(path, "wb") as fh:
            fh.write(payload)
        return len(payload)

    def test_export_then_readback(self):
        tmp = tempfile.mkdtemp(prefix="offflow_verify_")
        path = os.path.join(tmp, "简历.pdf")
        self.assertGreater(self._write(SAMPLE, path), 1000)
        result = pdf_verify.verify_exported_pdf(path, SAMPLE)
        if result.degraded:
            # 没装 PDF 文本引擎：只能证明「如实报告自己没校验」，不能假装通过
            self.assertIn("未能回读", result.summary())
            print("    [注] 本机无 PDF 文本引擎，只验证了降级语义")
            return
        self.assertTrue(result.checked)
        self.assertTrue(result.ok, result.summary() + " | " + str(result.missing[:3]))
        self.assertGreaterEqual(result.coverage, MIN_COVERAGE)

    def test_reverse_thin_pdf_reports_missing(self):
        """反向对照放在**产物侧**：内容少印一大半，回读必须报缺失。"""
        if not _engine_available():
            self.skipTest("本机没有 PDF 文本引擎，无法回读")
        thin = {"name": SAMPLE["name"], "position": SAMPLE["position"],
                "contact": SAMPLE["contact"]}
        tmp = tempfile.mkdtemp(prefix="offflow_verify_thin_")
        path = os.path.join(tmp, "薄.pdf")
        self._write(thin, path)
        # 用**完整**内容当锚点去查这份薄产物
        result = pdf_verify.verify_exported_pdf(path, SAMPLE)
        self.assertFalse(result.ok, "薄产物必须判不合格")
        self.assertGreater(len(result.missing), 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
