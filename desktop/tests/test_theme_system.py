#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""「跟随系统主题」的契约与接线判据。

要钉住的只有一件事，但它很容易被破坏
------------------------------------
``resolve_theme`` 的契约是：**返回值一定不是 "system"**。

配置里可以存 ``"system"``，但任何消费点拿到的都必须是 ``"light"`` / ``"dark"``。
原因是消费点散得到处都是，而它们大多是这种写法::

    if theme == "dark": ... else: 浅色

一旦有哪条路漏了解析、直接拿到 ``"system"``，那个二元判断会**静默掉回浅色**：
系统是深色时，QSS 变深了、手绘图标与状态色板却还按浅色画，界面就会花 ——
而且不报错、不崩溃，只能靠肉眼发现。

所以判据分两层：
  · **契约层**：`resolve_theme` 永不返回 "system"；QSS 与图标两条路都过解析；
  · **接线层**：系统主题变化有监听、切换主题三处（QSS / tokens / 图标）
    一次同步、设置页下拉的兜底落在「浅色」而不是下标 0。

反向对照：
  ① 绕开解析的二元实现（``x == "dark"``）在系统深色时会判成浅色；
  ② 下拉兜底若用 ``setCurrentIndex(0)``，会静默把用户改成「跟随系统」。

运行：python tests/test_theme_system.py
"""

import ast
import os
import sys
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from PySide6.QtWidgets import QApplication                        # noqa: E402

_APP = QApplication.instance() or QApplication([])

from offerflow_desktop.ui import theme, tokens                     # noqa: E402


def _dotted(node) -> set:
    """收集节点里的点号链（``tokens.set_theme`` / ``self.apply_theme``）。

    为什么要这么绕：直接对 `ast.unparse` 出来的源码做子串匹配，判据就绑在
    **引号风格**上了 —— 同一份代码写成 `'system'` 与 `"system"` 会让判据
    一绿一红（第一次写这条就踩了）。按 AST 取名字与常量才是稳的。
    """
    out = set()
    for n in ast.walk(node):
        if isinstance(n, ast.Attribute):
            parts = []
            cur = n
            while isinstance(cur, ast.Attribute):
                parts.append(cur.attr)
                cur = cur.value
            if isinstance(cur, ast.Name):
                parts.append(cur.id)
            out.add(".".join(reversed(parts)))
    return out


def _string_constants(node) -> set:
    return {n.value for n in ast.walk(node)
            if isinstance(n, ast.Constant) and isinstance(n.value, str)}


def _func_node(rel_path: str, cls: str, name: str) -> ast.FunctionDef:
    path = os.path.join(_ROOT, "offerflow_desktop", rel_path)
    with open(path, encoding="utf-8") as fh:
        tree = ast.parse(fh.read())
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == cls:
            for fn in node.body:
                if isinstance(fn, ast.FunctionDef) and fn.name == name:
                    return fn
    raise AssertionError("没找到 %s.%s" % (cls, name))


class TestResolveContract(unittest.TestCase):

    def test_never_returns_system(self):
        for value in (None, "", "system", "light", "dark", "SYSTEM",
                      "跟随系统", "dark ", 0, [], {"a": 1}):
            with self.subTest(value=value):
                got = theme.resolve_theme(value)
                self.assertIn(got, ("light", "dark"),
                              "resolve_theme(%r) 返回了 %r" % (value, got))

    def test_explicit_values_are_respected(self):
        self.assertEqual(theme.resolve_theme("light"), "light")
        self.assertEqual(theme.resolve_theme("dark"), "dark")

    def test_system_follows_the_os(self):
        for os_theme in ("light", "dark"):
            with self.subTest(os_theme=os_theme):
                with mock.patch.object(theme, "system_theme",
                                       return_value=os_theme):
                    self.assertEqual(theme.resolve_theme("system"), os_theme)

    def test_unknown_value_falls_back_to_light(self):
        self.assertEqual(theme.resolve_theme("霓虹紫"), "light")

    def test_system_theme_never_raises_and_is_binary(self):
        for _ in range(2):
            got = theme.system_theme()          # 不 mock：走真实探测路径
            self.assertIn(got, ("light", "dark"))

    def test_system_theme_survives_probe_failure(self):
        """系统主题探测失败时必须给个确定值，不能让界面起不来。"""
        with mock.patch.object(theme, "QStyleHints", None, create=True):
            got = theme.system_theme()
        self.assertIn(got, ("light", "dark"))


class TestConsumersGoThroughResolution(unittest.TestCase):

    def test_qss_differs_between_themes(self):
        self.assertNotEqual(theme.current_qss("light"), theme.current_qss("dark"))

    def test_qss_uses_resolved_theme(self):
        with mock.patch.object(theme, "system_theme", return_value="dark"):
            self.assertEqual(theme.current_qss("system"), theme.current_qss("dark"))
        with mock.patch.object(theme, "system_theme", return_value="light"):
            self.assertEqual(theme.current_qss("system"), theme.current_qss("light"))

    def test_both_qss_and_icons_consult_resolve_theme(self):
        """接线判据：QSS 与手绘图标**两条路**都必须过 resolve_theme。

        用 call_count 而不是「源码里有没有这行」：把任一处解析删掉，
        计数立刻掉下来。这条正是「同一个坑只堵一半」的探针。
        """
        calls = []
        real = theme.resolve_theme

        def spy(value):
            calls.append(value)
            return real(value)

        with mock.patch.object(theme, "resolve_theme", side_effect=spy):
            theme.current_qss("system")
            tokens.set_theme("system")
            tokens.is_dark()
        self.assertGreaterEqual(
            len(calls), 2,
            "resolve_theme 只被调了 %d 次 —— 说明有消费点绕过了它" % len(calls))
        self.assertIn("system", calls)
        tokens.set_theme(None)

    def test_is_dark_agrees_with_qss_choice(self):
        for stored, os_theme in (("system", "dark"), ("system", "light"),
                                 ("dark", "light"), ("light", "dark")):
            with self.subTest(stored=stored, os_theme=os_theme):
                with mock.patch.object(theme, "system_theme",
                                       return_value=os_theme):
                    tokens.set_theme(stored)
                    dark_qss = theme.current_qss(stored) == theme.current_qss("dark")
                    self.assertEqual(tokens.is_dark(), dark_qss,
                                     "QSS 与图标对明暗的判断不一致（界面会花）")
        tokens.set_theme(None)

    def test_reverse_naive_comparison_is_wrong(self):
        """反向对照：不解析的 ``x == "dark"`` 在系统深色时会判成浅色。"""
        with mock.patch.object(theme, "system_theme", return_value="dark"):
            tokens.set_theme("system")
            self.assertTrue(tokens.is_dark(), "系统深色时 is_dark() 必须是 True")
            naive = (lambda t: t == "dark")("system")
            self.assertFalse(naive, "这就是「不解析」会踩的坑，判据必须看得见差别")
        tokens.set_theme(None)


class TestSettingsPage(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        from offerflow_desktop.ui.pages import system_settings as ss
        cls.ss = ss

    def _page(self, stored_theme):
        mw = mock.MagicMock()
        with mock.patch.object(self.ss.db, "get_system_config",
                               return_value={"theme": stored_theme}), \
                mock.patch.object(self.ss.db, "set_system_config"), \
                mock.patch.object(self.ss, "_set_auto_start", return_value=False):
            page = self.ss.SystemSettingsPage(mw)
        self.addCleanup(page.setParent, None)
        return page, mw

    def test_combo_offers_follow_system_first(self):
        page, _mw = self._page("light")
        data = [page.theme_combo.itemData(i) for i in range(page.theme_combo.count())]
        self.assertEqual(data[0], "system", "「跟随系统」应在首位")
        self.assertEqual(set(data), {"system", "light", "dark"})

    def test_known_value_is_selected(self):
        for stored in ("light", "dark", "system"):
            with self.subTest(stored=stored):
                page, _mw = self._page(stored)
                self.assertEqual(page.theme_combo.currentData(), stored)

    def test_reverse_unknown_value_falls_back_to_light_not_index_zero(self):
        """反向对照：兜底若写成 `setCurrentIndex(0)`，会把用户静默改成「跟随系统」。

        下标 0 现在是「跟随系统」—— 这个副作用没有任何提示，
        用户只会觉得「我的设置自己变了」。
        """
        page, _mw = self._page("霓虹紫")
        self.assertEqual(page.theme_combo.currentData(), "light",
                         "未知值必须落到「浅色」，不能落到下标 0")
        self.assertNotEqual(page.theme_combo.currentData(), "system")

    def test_changing_theme_delegates_to_window(self):
        """三处同步（QSS / tokens / 图标）统一交给 apply_theme，页面不自己写。"""
        page, mw = self._page("light")
        with mock.patch.object(self.ss.db, "set_system_config") as save:
            page.theme_combo.setCurrentIndex(
                page.theme_combo.findData("dark"))
        self.assertTrue(save.called, "选择应被持久化")
        self.assertTrue(mw.apply_theme.called, "应交给窗口统一应用")
        self.assertEqual(mw.apply_theme.call_args[0][0], "dark")


class TestMainWindowWiring(unittest.TestCase):

    def test_apply_theme_syncs_all_three(self):
        node = _func_node("ui/main_window.py", "MainWindow", "apply_theme")
        dotted = _dotted(node)
        self.assertIn("setStyleSheet", {d.split(".")[-1] for d in dotted},
                      "没同步 QSS")
        self.assertIn("tokens.set_theme", dotted,
                      "没同步手绘色/图标用的主题状态")
        self.assertIn("reapply_icons", {d.split(".")[-1] for d in dotted},
                      "没同步侧栏图标")

    def test_window_watches_os_theme_changes(self):
        """监听必须装在**主窗口**上，不能装在入口脚本里。

        入口脚本（main.py）的成员不在任何用例的覆盖范围内 ——
        放那儿的东西坏了没人知道（v2.2.24 就是这么全绿发出去的）。
        """
        node = _func_node("ui/main_window.py", "MainWindow", "_watch_system_theme")
        dotted = _dotted(node)
        self.assertIn("colorSchemeChanged", {d.split(".")[-1] for d in dotted},
                      "没连系统主题变化信号")

    def test_scheme_handler_only_acts_when_configured_to_follow(self):
        """系统主题变了要**先确认**配置是「跟随系统」，才动界面。

        漏了这一步的话，用户显式选了「深色」，系统一切换浅色就被覆盖 ——
        设置看起来「自己变了」。
        """
        node = _func_node("ui/main_window.py", "MainWindow",
                          "_on_system_scheme_changed")
        compares = [n for n in ast.walk(node) if isinstance(n, ast.Compare)]
        self.assertTrue(compares, "缺少「当前是否为跟随系统」的判断")
        compared = set()
        for cmp_node in compares:
            compared |= _string_constants(cmp_node)
        self.assertIn("system", compared, "判断的对象应当是 system")
        # 判断读的必须是**持久化的配置**，不是内存里的下拉框
        dotted = _dotted(node)
        self.assertTrue(any("get_system_config" in d for d in dotted),
                        "判断应读系统配置，而不是界面控件")


if __name__ == "__main__":
    unittest.main(verbosity=2)
