#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""统计视图判据的**反向对照**：把缺陷放回去，看用例是否真的变红。

为什么必须有这一步：全绿的用例只说明「当前代码能过它」，不说明
「换个错实现它会拦下来」。判据写歪了（看错方法、断言方向写反、
压根没覆盖那条分支）时用例照样全绿 —— 假绿比没写用例更危险。

做法：对 ``ui/charts.py`` / ``ui/pages/tracker.py`` 做**精确字符串替换**
（并校验锚点唯一）→ 跑指定用例 → **无论结果都还原**。

⚠ 读写都必须 ``newline=""``：仓库约定 LF、``.gitattributes`` 是 ``* -text``、
打包产物按字节 sha256 校验。用默认文本模式回写，Python 在 Windows 会把
``\\n`` 翻成 ``os.linesep``，整个文件变成 CRLF，产物哈希跟着变。

运行：python tests/_mutate_stats_check.py
"""

import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
PKG = os.path.join(ROOT, "offerflow_desktop")
CHARTS = os.path.join(PKG, "ui", "charts.py")
TRACKER = os.path.join(PKG, "ui", "pages", "tracker.py")
TEST = "test_tracker_stats.py"

# `all=True` 表示该片段在文件里出现多次时的替换策略（默认必须唯一）
MUTATIONS = [
    {
        "id": "M1",
        "desc": "条形图不过滤 0 值（画出没数据的条）",
        "file": CHARTS,
        "before": '''    def visible_items(self) -> List[Item]:
        """值为 0 的不画（对齐网页版 ``funnelData.filter(count > 0)``）。"""
        return [(lab, val, col) for lab, val, col in self._items if val > 0]''',
        "after": '''    def visible_items(self) -> List[Item]:
        """值为 0 的不画（对齐网页版 ``funnelData.filter(count > 0)``）。"""
        return [(lab, val, col) for lab, val, col in self._items if val >= 0]''',
        "case": "TestBarChartGeometry.test_zero_valued_rows_are_dropped",
    },
    {
        "id": "M2",
        "desc": "条形定标用总和而不是最大值（条长不再成比例）",
        "file": CHARTS,
        "before": "        top = max(vals) if vals else 0.0\n"
                  "        if top <= 0:\n"
                  "            return {lab: 0.0 for lab, _, _ in self.visible_items()}\n"
                  "        return {lab: v / top for lab, v, _ in self.visible_items()}",
        "after": "        top = sum(vals) if vals else 0.0\n"
                 "        if top <= 0:\n"
                 "            return {lab: 0.0 for lab, _, _ in self.visible_items()}\n"
                 "        return {lab: v / top for lab, v, _ in self.visible_items()}",
        "case": "TestBarChartGeometry.test_bar_length_is_proportional_to_value",
    },
    {
        "id": "M3",
        "desc": "环形张角封顶 180°（一圈只画半圈）",
        "file": CHARTS,
        "before": "            span = 360.0 * value / total",
        "after": "            span = 180.0 * value / total",
        "case": "TestDonutGeometry.test_span_is_proportional_and_sums_to_full_circle",
    },
    {
        "id": "M4",
        "desc": "构造期就发统计请求（重建窗口时会卡住）",
        "file": TRACKER,
        "before": "        layout.addWidget(self.tabs, 1)\n        self._refresh()",
        "after": "        layout.addWidget(self.tabs, 1)\n"
                 "        self._refresh()\n        self._load_stats()",
        "case": "TestStatsLoadTiming.test_ctor_does_not_fetch_stats",
    },
    {
        "id": "M5",
        "desc": "页面显示时不加载统计（懒加载入口被摘掉）",
        "file": TRACKER,
        "before": "        if self._stats_dirty and self._stats_page is not None:\n"
                  "            self._load_stats()",
        "after": "        if False:\n            self._load_stats()",
        "case": "TestStatsLoadTiming.test_first_show_fetches_once",
    },
    {
        "id": "M6",
        "desc": "写操作后不标脏、也不重载（改动看不到）",
        "file": TRACKER,
        "before": "        self._stats_dirty = True\n"
                  "        if self._stats_page is not None and \\\n"
                  "                self.tabs.currentWidget() is self._stats_page:\n"
                  "            self._load_stats()",
        "after": "        pass",
        "case": "TestStatsLoadTiming.test_refresh_off_tab_only_marks_dirty",
    },
    {
        "id": "M7",
        "desc": "卡片数字不刷新（永远显示占位破折号）",
        "file": TRACKER,
        "before": "            tile.value_label.setText(values.get(key, \"—\"))",
        "after": "            pass",
        "case": "TestStatsTab.test_cards_show_server_numbers",
    },
    {
        "id": "M8",
        "desc": "状态分布改用漏斗那套阶段（终局状态被丢掉）",
        "file": TRACKER,
        "before": "        self.status_chart.set_items([\n"
                  "            (STATUS_LABELS.get(k, k), counts.get(k, 0), color_of(k))\n"
                  "            for k in order\n"
                  "        ])",
        "after": "        self.status_chart.set_items([\n"
                 "            (STATUS_LABELS.get(k, k), counts.get(k, 0), color_of(k))\n"
                 "            for k in FUNNEL_STEPS\n"
                 "        ])",
        "case": "TestStatsTab.test_distribution_uses_status_order_including_terminal_states",
    },
    {
        "id": "M9",
        "desc": "辅助色不分主题（深色下画浅色边框）",
        "file": CHARTS,
        "before": '        "empty_border": "#3a3a46" if dark else tokens.C.BORDER,',
        "after": '        "empty_border": tokens.C.BORDER,',
        "case": "TestChartRendering.test_reverse_rendered_canvas_never_shows_the_other_theme_border",
    },
    {
        "id": "M10",
        "desc": "手绘画布不监听主题变化（切深色后还是浅色）",
        "file": CHARTS,
        "before": "        if event.type() == QEvent.Type.StyleChange:\n"
                  "            self.update()",
        "after": "        if event.type() == QEvent.Type.Show:\n"
                 "            self.update()",
        "case": "TestChartRendering.test_reverse_repaint_is_triggered_by_style_change",
        "all": True,
    },
]


def run_case(case: str) -> bool:
    proc = subprocess.run(
        [sys.executable, "-u", os.path.join(HERE, TEST), case],
        cwd=ROOT, capture_output=True, text=True)
    return proc.returncode == 0


def main() -> int:
    sources = {}
    for path in {m["file"] for m in MUTATIONS}:
        with open(path, encoding="utf-8", newline="") as fh:
            sources[path] = fh.read()

    def _write(path: str, text: str) -> None:
        with open(path, "w", encoding="utf-8", newline="") as fh:
            fh.write(text)

    bad = []
    try:
        for mut in MUTATIONS:
            num, desc = mut["id"], mut["desc"]
            original = sources[mut["file"]]
            n = original.count(mut["before"])
            if n == 0:
                print("[%s] 锚点没匹配上，变异无效：%s" % (num, desc))
                bad.append("%s 锚点失配" % num)
                continue
            if n > 1 and not mut.get("all"):
                print("[%s] 锚点在文件里出现 %d 次，变异落点不确定：%s" % (num, n, desc))
                bad.append("%s 锚点不唯一" % num)
                continue
            if not run_case(mut["case"]):
                print("[%s] 基线就是红的，无法判断变异是否被拦住：%s" % (num, mut["case"]))
                bad.append("%s 基线红" % num)
                continue

            mutated = original.replace(mut["before"], mut["after"])
            _write(mut["file"], mutated)
            try:
                passed = run_case(mut["case"])
            finally:
                _write(mut["file"], original)

            if passed:
                print("[%s] XX 变异后用例仍然通过 —— 判据是空靶：%s" % (num, desc))
                bad.append(num)
            else:
                print("[%s] OK 变异被判据拦下：%s" % (num, desc))
    finally:
        for path, original in sources.items():
            _write(path, original)

    print()
    if bad:
        print("反向对照未通过：%s" % ", ".join(bad))
        return 1
    print("反向对照全部通过（%d 处变异都被判据拦下）" % len(MUTATIONS))
    return 0


if __name__ == "__main__":
    sys.exit(main())
