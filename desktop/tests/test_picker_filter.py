#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""版式选择对话框的「筛选 / ATS 视角 / 分区顺序拖放」判据。

为什么需要
----------
这三件事都是「列表看起来对、其实不对」的高发区：

  · **筛选**：如果按「叠加外观后的有效模板」筛，用户把字号拖到 85% 会让
    「ATS 安全」的结果直接**空掉** —— 列表看起来像坏了。所以判据必须用
    出厂模板，而且要用一条**反向对照**把这个区别钉住。
  · **ATS 视角**：要回读**缓存里那一份** PDF 字节。重新渲染一次不仅慢，
    还可能和预览图不是同一份 —— 那就成了另一种「假一致」。
  · **拖放排序**：`rowsMoved` 是在模型搬运过程中发出的，此刻立刻重建列表
    可能打断它自己；所以判据要等到事件循环下一拍再看结果。

运行：python tests/test_picker_filter.py
"""

import os
import sys
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from PySide6.QtCore import QModelIndex, Qt                          # noqa: E402
from PySide6.QtWidgets import QApplication                          # noqa: E402

_APP = QApplication.instance() or QApplication([])

from offerflow_desktop.core.sections import (                       # noqa: E402
    BODY_SECTIONS, SECTION_LABELS, normalize_section_order,
)
from offerflow_desktop.services import templates                    # noqa: E402
from offerflow_desktop.ui.template_picker import (                  # noqa: E402
    TemplatePickerDialog, _extract_pdf_text,
)

SAMPLE = {
    "name": "张三",
    "position": "Python 后端",
    "contact": {"phone": "13800000000", "email": "zhangsan@example.com"},
    "summary": "三年后端开发经验，主导过订单服务重构。",
    "education": [{"school": "某大学", "major": "计算机", "degree": "本科",
                   "start": "2019-09", "end": "2023-06"}],
    "experiences": [{"name": "某公司", "role": "后端工程师", "start": "2023-07",
                     "bullets": ["负责订单服务重构，QPS 提升 3 倍"]}],
    "skills": ["Python", "FastAPI", "PostgreSQL"],
}


class _PickerCase(unittest.TestCase):
    """只构造一次对话框：每次构造都要真渲染一份 PDF 预览，太慢。"""

    dlg = None

    @classmethod
    def setUpClass(cls):
        cls.dlg = TemplatePickerDialog(SAMPLE)

    @classmethod
    def tearDownClass(cls):
        cls.dlg.setParent(None)
        cls.dlg = None

    def setUp(self):
        # 每个用例从「全部 + 默认顺序」出发，避免互相污染
        self.dlg._on_filter_changed("all")
        self.dlg.selected_order = normalize_section_order(BODY_SECTIONS)
        self.dlg._refresh_order_list()


class TestFilter(_PickerCase):

    def _keys(self):
        out = []
        for i in range(self.dlg.list.count()):
            key = self.dlg.list.item(i).data(Qt.ItemDataRole.UserRole)
            if key:
                out.append(key)
        return out

    def test_filter_counts_match_factory_templates(self):
        """期望值从 TEMPLATES **现算**，不写死数字。

        写死的话，将来加一套模板这条用例就假红，然后被人改数字而不是看逻辑。
        """
        self.dlg._on_filter_changed("all")
        self.assertEqual(len(self._keys()), len(templates.TEMPLATES))

        self.dlg._on_filter_changed("single")
        want = [t.key for t in templates.TEMPLATES if t.columns == 1]
        self.assertEqual(self._keys(), want)

        self.dlg._on_filter_changed("ats")
        want = [t.key for t in templates.TEMPLATES if t.ats_safe]
        self.assertEqual(self._keys(), want)

    def test_ats_filter_is_not_empty(self):
        """至少要有一套自称 ATS 安全的模板，否则这个筛选项没有意义。"""
        self.dlg._on_filter_changed("ats")
        self.assertTrue(self._keys(), "「ATS 安全」筛出来是空的")

    def test_filter_survives_extreme_appearance(self):
        """反向对照：筛选判据必须用**出厂模板**，不能被外观影响。

        若改成「叠加外观后的有效模板」，把字号拖到下限会让 ATS 安全筛选
        直接空掉 —— 用户会以为列表坏了。这条判据就是钉住这一点。
        """
        self.dlg._on_filter_changed("ats")
        before = self._keys()
        self.assertTrue(before)
        saved = self.dlg.selected_appearance
        try:
            self.dlg.selected_appearance = templates.ResumeAppearance(
                font_scale=templates.ResumeAppearance.MIN_SCALE)
            self.dlg._refresh_template_list()
            self.assertEqual(self._keys(), before,
                             "筛选结果被外观影响了 —— 说明判据用错了模板对象")
            self.dlg.selected_appearance = templates.ResumeAppearance(
                font_scale=templates.ResumeAppearance.MAX_SCALE)
            self.dlg._refresh_template_list()
            self.assertEqual(self._keys(), before)
        finally:
            self.dlg.selected_appearance = saved

    def test_placeholder_has_no_key(self):
        """空结果要能区分「筛空了」与「筛出个没名字的东西」。

        当前三档筛选都筛得出东西，所以「筛空」这条路径只能靠**劫持判据**
        造出来（`_template_passes` 恒 False）—— 这不是取巧：
        它测的正是「列表空时 UI 会变成什么样」，与筛选规则改了无关。
        """
        with mock.patch.object(self.dlg, "_template_passes", return_value=False):
            self.dlg._refresh_template_list()
            self.assertEqual(self.dlg.list.count(), 1)
            self.assertIsNone(
                self.dlg.list.item(0).data(Qt.ItemDataRole.UserRole),
                "占位项不能带 key，否则会被误当成选中项")
        self.dlg._refresh_template_list()
        self.assertGreater(self.dlg.list.count(), 1, "撤销劫持后应恢复正常")

    def test_unknown_filter_behaves_like_all(self):
        """未知筛选值回退成「不过滤」而不是「筛空」。

        筛空会让用户以为列表坏了；不过滤只是没生效，看得出是配置问题。
        """
        self.dlg._filter = "将来才有的筛选项"
        try:
            self.dlg._refresh_template_list()
            self.assertEqual(len(self._keys()), len(templates.TEMPLATES))
        finally:
            self.dlg._filter = "all"
            self.dlg._refresh_template_list()

    def test_switching_filter_does_not_silently_change_selection(self):
        """被筛掉的选中项不该被静默换成别的（用户会以为自己换了模板）。"""
        self.dlg._on_filter_changed("all")
        self.dlg._select(templates.TEMPLATES[0].key)
        original = self.dlg.selected_key
        self.dlg._on_filter_changed("ats")
        if original in self._keys():
            self.assertEqual(self.dlg.selected_key, original)
        else:
            self.assertEqual(self.dlg.list.currentRow(), -1,
                             "选中项被筛掉时应清空高亮，而不是改选另一套")


class TestAtsView(_PickerCase):

    def test_view_reads_the_cached_pdf_bytes(self):
        self.dlg._select(self.dlg.selected_key)
        self.dlg._refresh_ats_text()
        cached = self.dlg._cache.get(self.dlg._cache_key())
        self.assertIsNotNone(cached, "应该已经渲染并缓存过一份 PDF")
        payload = cached[2]
        self.assertTrue(payload.startswith(b"%PDF"), "缓存里要留着真正的字节")

    def test_view_shows_real_extracted_text(self):
        self.dlg._select(self.dlg.selected_key)
        self.dlg._refresh_ats_text()
        text = self.dlg.ats_view.toPlainText()
        note = self.dlg.ats_note_label.text()
        if not text.strip():
            # 本机没装 PDF 文本引擎：只能证明「如实说明读不到」，不能假装通过
            self.assertTrue(note.strip(), "读不到也必须给出说明")
            print("    [注] 本机无 PDF 文本引擎，ATS 视角只验证了降级语义")
            return
        plain = text.replace(" ", "")
        self.assertIn("张三", plain)
        self.assertIn("Python", plain)
        self.assertIn("某大学", plain)

    def test_extract_pdf_text_is_safe_on_garbage(self):
        for payload in (b"", b"not a pdf", b"%PDF-1.4 \xff\xfe"):
            with self.subTest(payload=payload):
                text, note = _extract_pdf_text(payload)
                self.assertIsInstance(text, str)
                self.assertIsInstance(note, str)
                if not text:
                    self.assertTrue(note, "失败必须给原因，不能静默返回空串")

    def test_ats_notes_are_computed_not_hardcoded(self):
        """ATS 标注必须**现算**：同一模板缩字号后标注应从空变非空。"""
        tpl = templates.get_template(self.dlg.selected_key)
        base = templates.apply_appearance(tpl, templates.ResumeAppearance())
        tight = templates.apply_appearance(tpl, templates.ResumeAppearance(
            font_scale=templates.ResumeAppearance.MIN_SCALE))
        self.assertNotEqual(base.body_pt, tight.body_pt, "缩字号应真的改到字号")
        self.assertNotEqual(list(base.ats_notes), list(tight.ats_notes),
                            "标注没随字号变 —— 说明是写死的而不是现算的")


class TestOrderDrag(_PickerCase):

    def _rows(self):
        return [self.dlg.order_list.item(i).data(Qt.ItemDataRole.UserRole)
                for i in range(self.dlg.order_list.count())]

    def test_list_matches_selected_order(self):
        self.assertEqual(self._rows(), list(self.dlg.selected_order))
        self.assertEqual([self.dlg.order_list.item(i).text()
                          for i in range(self.dlg.order_list.count())],
                         [SECTION_LABELS[n] for n in self.dlg.selected_order])

    def test_drag_moves_row_and_updates_order(self):
        """拖放改顺序。

        注意 `moveRow(parent, srcRow, parent, destChild)` 的语义是
        **插到 destChild 之前**（不是「插到第 destChild 行」）。所以
        `moveRow(0 → 3)` 的落点是索引 **2**（第 0 行被抽走后，原来的第 3 行
        前移了一位）。第一版按「插到第 3 行」写，断言直接对不上 ——
        这种地方只能按实测语义写，不能按直觉写。
        """
        model = self.dlg.order_list.model()
        before = list(self.dlg.selected_order)
        self.assertTrue(model.moveRow(QModelIndex(), 0, QModelIndex(), 3),
                        "模型的 moveRow 应该成功")
        self.dlg._collect_order_after_drag()
        after = list(self.dlg.selected_order)
        self.assertNotEqual(after, before, "拖放后顺序应变")
        self.assertEqual(after[2], before[0], "第 0 行应落在索引 2")
        self.assertEqual(after[:2], before[1:3], "被跨过的两行整体前移")
        self.assertEqual(after[3:], before[3:], "其余不变")
        self.assertEqual(sorted(after), sorted(before), "只能换位置，不能丢项")

    def test_collect_without_change_does_not_rerender(self):
        """顺序没变时不该触发一次没必要的重渲染（每次都要真渲染一份 PDF）。"""
        before = list(self.dlg.selected_order)
        with mock.patch.object(self.dlg, "_select") as sel:
            self.dlg._collect_order_after_drag()
        self.assertEqual(list(self.dlg.selected_order), before)
        self.assertFalse(sel.called, "顺序没变却重渲染了")

    def test_moved_row_keeps_its_key(self):
        """列表项必须带着分区 key，否则拖完只剩中文标题、顺序无从落地。"""
        rows = self._rows()
        self.assertTrue(all(rows), "有列表项没带 key：%r" % (rows,))
        self.assertEqual(len(set(rows)), len(rows), "分区 key 不该重复")
        self.assertEqual(set(rows), set(BODY_SECTIONS), "key 必须是已知分区")

    def test_reverse_labels_are_not_accepted_as_keys(self):
        """反向对照：中文**标题**不是合法 key，绝不能当顺序用。

        这条不是空转 —— 它证明「列表里存的必须是 key」这个要求有落地价值：
        若哪天有人把 `_refresh_order_list` 改成存标题，收集回来的顺序就会
        被 `normalize_section_order` 整批丢弃、静默变回默认序，
        用户看到的是「拖了半天没反应」。
        """
        reversed_keys = list(reversed(BODY_SECTIONS))
        self.assertEqual(normalize_section_order(reversed_keys), reversed_keys,
                         "合法 key 的顺序应当被尊重")
        permuted_labels = [SECTION_LABELS[k] for k in reversed_keys]
        self.assertNotEqual(normalize_section_order(permuted_labels), reversed_keys,
                            "中文标题被当成 key 用了")
        # 全被丢弃后按默认序补齐，长度不变量仍要成立
        self.assertEqual(normalize_section_order(permuted_labels),
                         list(BODY_SECTIONS))


if __name__ == "__main__":
    unittest.main(verbosity=2)
