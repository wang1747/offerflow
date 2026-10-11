#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ATS 前后对比判据的**反向对照**：把缺陷放回去，看用例是否真的变红。

为什么必须有这一步：全绿的用例只说明「当前代码能过它」，不说明
「换个错实现它会拦下来」。判据写歪了（看错方法、断言方向写反、
压根没覆盖那条分支）时用例照样全绿 —— 假绿比没写用例更危险。

本轮的两次自查就出在这里：第一版「量程」判据拿了 (52, 100) 做输入，
最大值的 100 恰好等于满分，两种量程算出来一样，判据空转；同一条反向
对照也因此失败。换成 (52, 80) 才真正切中要害。

做法：对 ``api_client.py`` / ``ui/charts.py`` / ``ui/pages/resume_optimize.py``
做**精确字符串替换**（并校验锚点唯一）→ 跑指定用例 → **无论结果都还原**。

⚠ 读写都必须 ``newline=""``：仓库约定 LF、``.gitattributes`` 是 ``* -text``、
打包产物按字节 sha256 校验。用默认文本模式回写，Python 在 Windows 会把
``\\n`` 翻成 ``os.linesep``，整个文件变成 CRLF，产物哈希跟着变。

运行：python tests/_mutate_ats_check.py
"""

import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
PKG = os.path.join(ROOT, "offerflow_desktop")
API = os.path.join(PKG, "api_client.py")
CHARTS = os.path.join(PKG, "ui", "charts.py")
PAGE = os.path.join(PKG, "ui", "pages", "resume_optimize.py")
THEME = os.path.join(PKG, "ui", "theme.py")
TEST = "test_resume_optimize_ats.py"

# `all=True` 表示该片段在文件里出现多次时的替换策略（默认必须唯一）
MUTATIONS = [
    {
        "id": "M1",
        "desc": "「没有 ATS 数据」被补成 0 分（网页版 ?? {score:0} 的做法）",
        "file": API,
        "before": "    if not isinstance(v, dict):\n        return None",
        "after": "    if not isinstance(v, dict):\n"
                 "        return {\"score\": 0, \"issues\": [], \"suggestions\": [],\n"
                 "                \"matched_keywords\": [], \"missing_keywords\": []}",
        "case": "TestAtsNormalize.test_missing_or_broken_payload_reads_as_none",
    },
    {
        "id": "M2",
        "desc": "0 分被当成「没有数据」（真实结论被藏起来）",
        "file": API,
        "before": "    if not 0 <= score <= 100:\n        return None",
        "after": "    if not 0 < score <= 100:\n        return None",
        "case": "TestAtsNormalize.test_zero_score_is_real_data_not_missing",
    },
    {
        "id": "M3",
        "desc": "逐项 None 不补空列表（页面 for 一下就是 TypeError）",
        "file": API,
        "before": '        "issues": [str(x) for x in _as_list(v.get("issues"))],',
        "after": '        "issues": v.get("issues"),',
        "case": "TestAtsNormalize.test_none_fields_become_empty_lists",
    },
    {
        "id": "M4",
        "desc": "对比条改按「本组最大值」归一（量程不再固定 100）",
        "file": CHARTS,
        "before": '        """条长占满格的比例 = 分数 / 100（量程固定，**不**按最大值归一）。"""\n'
                  "        return {lab: (0.0 if v is None else v / self.FULL) "
                  "for lab, v, _c in self._rows}",
        "after": '        """条长占满格的比例 = 分数 / 100（量程固定，**不**按最大值归一）。"""\n'
                 "        vals = [v for _l, v, _c in self._rows if v is not None]\n"
                 "        top = max(vals) if vals else 0.0\n"
                 "        return {lab: (0.0 if v is None or top <= 0 else v / top)\n"
                 "                for lab, v, _c in self._rows}",
        "case": "TestScoreCompareGeometry.test_scale_is_fixed_at_hundred_not_the_group_max",
    },
    {
        "id": "M5",
        "desc": "绘制时也按本组最大值定标（画出来两条一样长）",
        "file": CHARTS,
        "before": "                   max(r * 2, avail * (value / self.FULL)), self.BAR_H), r, r)",
        "after": "                   max(r * 2, avail * (value / max(\n"
                 "                       v for _l, v, _c in self._rows))), self.BAR_H), r, r)",
        "case": "TestBarRendering.test_two_bars_render_at_proportionally_different_lengths",
    },
    {
        "id": "M6",
        "desc": "只给一条分数也算「有数据」（不同尺的两条放一起比）",
        "file": CHARTS,
        "before": "        return bool(self._rows) and all(v is not None "
                  "for _lab, v, _c in self._rows)",
        "after": "        return bool(self._rows) and any(v is not None "
                 "for _lab, v, _c in self._rows)",
        "case": "TestScoreCompareGeometry.test_half_filled_chart_is_not_data",
    },
    {
        "id": "M7",
        "desc": "差值缺失时返回 0（页面会显示「分数持平」）",
        "file": CHARTS,
        "before": "        if not self.has_data():\n            return None",
        "after": "        if not self.has_data():\n            return 0.0",
        "case": "TestScoreCompareGeometry.test_delta_sign_and_missing_propagation",
    },
    {
        "id": "M8",
        "desc": "页面在 ats 缺失时照补 0 分并显示卡片",
        "file": PAGE,
        "before": "        if not ats or not ats_after:\n"
                  "            self.ats_card.setVisible(False)\n"
                  "            return",
        "after": '        ats = ats or {"score": 0, "issues": [], "suggestions": []}\n'
                 '        ats_after = ats_after or {"score": 0, "issues": [], "suggestions": []}',
        "case": "TestAtsCardWiring.test_reverse_render_without_ats_hides_the_card",
    },
    {
        "id": "M9",
        "desc": "差值正负判断写反（涨显示成跌）",
        "file": PAGE,
        "before": '        if delta > 0:\n'
                  '            cls, text = "text-success", f"↑ 提升 {delta} 分"\n'
                  '        elif delta < 0:\n'
                  '            cls, text = "text-danger", f"↓ 下降 {abs(delta)} 分"',
        "after": '        if delta < 0:\n'
                 '            cls, text = "text-success", f"↑ 提升 {abs(delta)} 分"\n'
                 '        elif delta > 0:\n'
                 '            cls, text = "text-danger", f"↓ 下降 {delta} 分"',
        "case": "TestAtsCardWiring.test_delta_class_switches_with_sign",
    },
    {
        "id": "M10",
        "desc": "清场时不清结论列表（上一轮的条目还挂在卡片上）",
        "file": PAGE,
        "before": "        self._clear_ats_list(self.ats_issues_layout)\n"
                  "        self._clear_ats_list(self.ats_suggestions_layout)",
        "after": "        pass",
        "case": "TestAtsCardWiring.test_clear_resets_everything",
    },
    {
        "id": "M11",
        "desc": "优化失败后不清 ATS（旧分数被当成这次的结论）",
        "file": PAGE,
        "before": "        # 失败时也要清：上一轮的 ATS 分数留在屏幕上，会被当成这次的结论\n"
                  "        self._clear_ats()\n"
                  "        QMessageBox.warning(self, \"优化失败\", msg)",
        "after": "        QMessageBox.warning(self, \"优化失败\", msg)",
        "case": "TestAtsCardWiring.test_reverse_failed_optimize_clears_previous_result",
    },
    {
        "id": "M12",
        "desc": "重跑期间不清场（结果还没回来就先挂着旧分数）",
        "file": PAGE,
        "before": "        self._clear_ats()\n"
                  "        self.overlay.show_over(",
        "after": "        self.overlay.show_over(",
        "case": "TestAtsCardWiring.test_rerun_clears_the_card_before_it_finishes",
    },
    {
        "id": "M13",
        "desc": "状态行在没有 ATS 时也拼「ATS 0 → 0」",
        "file": PAGE,
        "before": '        ats_text = f" · ATS {ats[\'score\']} → {ats_after[\'score\']}" '
                  "if (ats and ats_after) else \"\"",
        "after": '        ats_text = (" · ATS %s → %s"\n'
                 '                    % ((ats or {}).get("score", 0), (ats_after or {}).get("score", 0)))',
        "case": "TestAtsStatusLine.test_reverse_status_line_stays_silent_without_ats",
    },
    {
        "id": "M14",
        "desc": "关键词标签不分状态（全涂成「已命中」的绿）",
        "file": PAGE,
        "before": "                self.ats_keyword_host.add(soft_chip(str(word), _hit_tone(kind)))",
        "after": '                self.ats_keyword_host.add(soft_chip(str(word), _hit_tone("matched")))',
        "case": "TestAtsCardWiring.test_keyword_chips_use_three_distinct_colours",
    },
    {
        "id": "M15",
        "desc": "三态色不跟主题换档（深底上用浅色值）",
        "file": PAGE,
        "before": "    light, dark = _HIT_TONES[kind]\n    return dark if tokens.is_dark() else light",
        "after": "    light, dark = _HIT_TONES[kind]\n    return light",
        "case": "TestAtsCardWiring.test_hit_tone_switches_with_the_theme",
    },
    {
        "id": "M16",
        "desc": "「优化前的问题」误取优化后的数据",
        "file": PAGE,
        "before": "        self._fill_ats_list(self.ats_issues_layout, ats.get(\"issues\") or [],\n"
                  "                            \"没有发现结构性问题\", \"text-warn\")",
        "after": "        self._fill_ats_list(self.ats_issues_layout, ats_after.get(\"issues\") or [],\n"
                 "                            \"没有发现结构性问题\", \"text-warn\")",
        "case": "TestAtsCardWiring.test_issues_and_suggestions_follow_the_payload",
    },
    {
        "id": "M17",
        "desc": "浅色主题下布局容器不声明透明（卡片里露出一块页面底色）",
        "file": THEME,
        "before": "   （charts.py 那几个）不受影响 —— 它们本来就不画 QSS 背景。 */\n"
                  'QWidget[class="flush"] { background: transparent; }',
        "after": "   （charts.py 那几个）不受影响 —— 它们本来就不画 QSS 背景。 */",
        "case": "TestBarRendering.test_card_has_no_page_background_leak",
    },
    {
        "id": "M18",
        "desc": "深色主题下布局容器不声明透明（深色卡片里露深色页面底）",
        "file": THEME,
        "before": "/* 纯布局容器：见浅色主题同名规则 */\n"
                  'QWidget[class="flush"] { background: transparent; }',
        "after": "/* 纯布局容器：见浅色主题同名规则 */",
        "case": "TestBarRendering.test_card_has_no_page_background_leak",
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
