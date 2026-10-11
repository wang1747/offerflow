# -*- coding: utf-8 -*-
"""薪资参考页「我手里的 offer → 算定位」的接线判据（B 方案，2026-10-10）。

背景：`core.salary_benchmark.locate_offer`（S1 四档定位，对齐
Salary-Negotiation-Skill 的 `get_market_comparison`）落地后**全仓零调用点**
—— 函数在、它的用例也在，但界面用不到。这是 E-09「有源码≠在运行」，本轮第二次
踩到，所以本文件验的是**接线本身**：

1. **解析层**：`parse_offer_amount` —— 用户随手写的 `18000` / `18,000` / `18k` /
   `1.8万` / `18000元/月` 都要认；读不出数字时必须返回 `None`（**不能退化成 0**
   去喂 `locate_offer`，那会静默走进 unknown，用户以为算过了）。
2. **控件接线层**：离屏构造页面 → 喂一个市场价结果 → 填 offer → 点「算定位」→
   断言**结论文案**（不是「出现过某个字符串」这种能被正文骗过的弱判据）。
3. **生命周期层**：结果区被清空（换基准版本时）控件会 `deleteLater`，引用必须
   同步置空 —— 否则再点就是摸已析构的 C++ 对象，Qt 不抛 Python 异常、直接崩进程。

（M11~M13 的反向对照已在开发期完成，脚本属内部资产未随仓库发布）。

运行：python tests/test_salary_offer_locator_ui.py
"""

import os
import sys
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

# ⚠️ `run_all.py` 是**直接执行** `python tests/xxx.py`，此时 sys.path[0] 是
# `tests/` 而不是仓库根，`import offerflow_desktop` 会直接 ImportError。
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from PySide6.QtWidgets import QApplication, QWidget                       # noqa: E402

_APP = QApplication.instance() or QApplication([])

from offerflow_desktop.core import salary_benchmark as sb                 # noqa: E402
from offerflow_desktop.ui.pages import salary as sal                      # noqa: E402

#: 喂给 `_on_reference` 的市场价结果。区间 16000~24000 是下面**全部档位断言的前提**，
#: 改这里必须同步改用例里的期望档位（见 `test_reference_fixture_is_the_premise`）。
_REF = {
    "degree": "硕士",
    "direction": "算法",
    "city_tier": "一线",
    "salary_range": {"low": 16000, "high": 24000},
    "suggest_target": 22000,
    "suggest_bottom": 17000,
    "market_basis": "硕士 × 算法 × 一线城市",
    "highlights": ["有大厂实习"],
    "analysis": "整体偏上，可以争 22k。",
    "sources": ["某机构 2026 届薪酬报告"],
    "caveats": ["样本有限"],
}


class _FakeMW(QWidget):
    """只提供页面构造时需要的宿主接口。"""

    def refresh_status(self):
        pass

    def refresh_ai_source(self):
        pass

    def navigate(self, *args, **kwargs):
        pass

    def set_status(self, *args, **kwargs):
        pass


class TestParseOfferAmount(unittest.TestCase):
    """解析层：用户随手写的月薪都要认，读不出来要老实返回 None。"""

    def test_plain_number(self):
        self.assertEqual(sb.parse_offer_amount("18000"), 18000)

    def test_thousands_separator(self):
        """★ 千分位必须整体吃掉。

        实测踩过：`18,000` 被读成 **18** —— 正则先在 `18` 处停下，逗号后的三位
        被当成「其它字符」忽略。用户看到的是「18 元 → 显著低于市场」，荒谬但
        不报错，属于最坏的一类错。
        """
        self.assertEqual(sb.parse_offer_amount("18,000"), 18000)

    def test_shorthand_units(self):
        self.assertEqual(sb.parse_offer_amount("18k"), 18000)
        self.assertEqual(sb.parse_offer_amount("18K"), 18000)
        self.assertEqual(sb.parse_offer_amount("1.8万"), 18000)

    def test_decorations_are_ignored(self):
        self.assertEqual(sb.parse_offer_amount("18000元/月"), 18000)
        self.assertEqual(sb.parse_offer_amount("税前 18000"), 18000)
        self.assertEqual(sb.parse_offer_amount("  18000  "), 18000)

    def test_fullwidth_digits(self):
        """中文输入法下很容易打出全角数字。"""
        self.assertEqual(sb.parse_offer_amount("１８０００"), 18000)

    def test_unreadable_returns_none_not_zero(self):
        """读不出来必须是 None —— 返回 0 会被 locate_offer 静默判成 unknown。"""
        for raw in ("", "   ", "abc", "大概两万吧", "万", "k", None):
            self.assertIsNone(sb.parse_offer_amount(raw), repr(raw))

    def test_non_positive_returns_none(self):
        self.assertIsNone(sb.parse_offer_amount("0"))
        self.assertIsNone(sb.parse_offer_amount("-5000"))


class _Base(unittest.TestCase):
    def setUp(self):
        # 前提自检：下面全部档位断言都建立在 _REF 的区间上
        self.assertEqual(_REF["salary_range"], {"low": 16000, "high": 24000})
        self.mw = _FakeMW()
        self.addCleanup(self.mw.deleteLater)

    def _page(self):
        page = sal.SalaryPage(self.mw)
        self.addCleanup(page.setParent, None)
        self.addCleanup(page.deleteLater)
        return page

    def _page_with_reference(self, ref=None):
        page = self._page()
        page._on_reference(dict(ref or _REF))
        return page


class TestOfferLocatorCard(_Base):
    """控件接线层：结果卡出现时才有定位块，点一下真出结论。"""

    def test_constructor_keeps_locator_absent(self):
        """构造期不搭定位块（它挂在市场价结果卡里，没有结果就没有它）。"""
        page = self._page()
        self.assertIsNone(page.offer_input)
        self.assertIsNone(page.offer_btn)
        self.assertIsNone(page.offer_result)
        self.assertIsNone(page._last_reference)

    def test_reference_builds_input_and_keeps_result_hidden(self):
        page = self._page_with_reference()
        self.assertIsNotNone(page.offer_input)
        self.assertIsNotNone(page.offer_btn)
        self.assertIsNotNone(page.offer_result)
        self.assertTrue(page.offer_result.isHidden(), "还没算就显示结论会误导")

    def test_click_locates_below_market(self):
        page = self._page_with_reference()
        page.offer_input.setText("12000")          # < 16000×0.9 = 14400
        page.offer_btn.click()
        text = page.offer_result.text()
        self.assertIn("显著低于市场", text)          # 断言结论值，不是「出现过某词」
        self.assertIn("12,000", text)
        self.assertIn("往上争取", text)
        self.assertEqual(page.offer_result.property("class"), "text-warn")

    def test_click_locates_market_rate(self):
        page = self._page_with_reference()
        page.offer_input.setText("18000")          # 落在 16000~24000 内
        page.offer_btn.click()
        text = page.offer_result.text()
        self.assertIn("符合市场水平", text)
        self.assertIn("6,000", text)               # gap_to_high = 24000-18000
        self.assertEqual(page.offer_result.property("class"), "hint")

    def test_click_locates_above_market(self):
        page = self._page_with_reference()
        page.offer_input.setText("30000")
        page.offer_btn.click()
        text = page.offer_result.text()
        self.assertIn("高于市场水平", text)
        self.assertIn("高出区间上限", text)
        self.assertIn("6,000", text)
        self.assertEqual(page.offer_result.property("class"), "text-success")

    def test_shorthand_inputs_all_land_on_same_band(self):
        """★ 各种写法必须落到同一档 —— 千分位/单位解析错了这里会露馅。"""
        page = self._page_with_reference()
        for raw in ("18000", "18,000", "18k", "1.8万", "18000元/月", "１８０００"):
            page.offer_btn.click()                 # 先点一次，确保上一轮有残留也不会误判
            page.offer_input.setText(raw)
            page.offer_btn.click()
            self.assertIn("符合市场水平", page.offer_result.text(), "写法：%s" % raw)

    def test_enter_key_also_locates(self):
        page = self._page_with_reference()
        page.offer_input.setText("18000")
        page.offer_input.returnPressed.emit()
        self.assertIn("符合市场水平", page.offer_result.text())

    def test_garbage_input_gets_no_band_conclusion(self):
        """读不出数字时只提示格式，**绝不能**给出档位结论。"""
        page = self._page_with_reference()
        page.offer_input.setText("大概两万吧")
        page.offer_btn.click()
        text = page.offer_result.text()
        self.assertIn("没读出数字", text)
        self.assertNotIn("市场水平", text)
        self.assertNotIn("市场位置", text)
        self.assertEqual(page.offer_result.property("class"), "text-danger")

    def test_empty_input_does_not_locate(self):
        page = self._page_with_reference()
        page.offer_btn.click()
        self.assertIn("没读出数字", page.offer_result.text())


class TestOfferLocatorLifecycle(_Base):
    """生命周期层：结果区清空后，引用必须同步置空。"""

    def test_clearing_result_drops_locator_references(self):
        page = self._page_with_reference()
        self.assertIsNotNone(page.offer_input)

        page._clear_reference_result()             # 换基准版本时会走这条

        self.assertIsNone(page._last_reference)
        self.assertIsNone(page.offer_input)
        self.assertIsNone(page.offer_btn)
        self.assertIsNone(page.offer_result)

    def test_locate_after_clearing_is_a_safe_noop(self):
        """★ 清空后再触发定位不能崩。

        控件已被 `deleteLater`，若引用没置空，`self.offer_input.text()` 摸的是
        已析构的 C++ 对象 —— 这种崩**没有 Python traceback**，只会让进程消失。
        """
        page = self._page_with_reference()
        page._clear_reference_result()
        page._locate_offer()                       # 不应抛异常
        self.assertIsNone(page.offer_result)

    def test_reference_without_range_stays_safe(self):
        """市场区间缺失/非法时不瞎判，给「先重算」的提示。"""
        page = self._page()
        broken = dict(_REF)
        broken["salary_range"] = {"low": 0, "high": 0}
        page._on_reference(broken)
        page.offer_input.setText("18000")
        page.offer_btn.click()
        self.assertIn("不可用", page.offer_result.text())
        self.assertEqual(page.offer_result.property("class"), "text-danger")


if __name__ == "__main__":
    unittest.main()
