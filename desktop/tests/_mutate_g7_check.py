#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""G7「优化页就地建档入口」判据的**反向对照**：把缺陷放回去，看用例是否真变红。

为什么必须有这一步：全绿的用例只说明「当前代码能过它」，不说明「换个错实现
它会拦下来」。本轮的判据里有两条天生容易写成空靶：

  * 「没有简历时引导条可见」—— 把 `setVisible` 整行删掉（永远不显示）它照样
    是绿的，只有当**两个方向都断言**（空时可见、有时收起）才拦得住；
  * 「导入按钮在页面上」—— 按钮在不在，跟点下去有没有走对流程是两件事。
    回调接成 no-op 时按钮照样能点、对话框照样弹，只有断言**接线**
    （宿主 + 回调身份）才抓得到。

「空态引导」这一项要**按机制分别变异**（M1–M4b）：它由两处代码共同保证 ——

  ① `__init__` 末尾摆的静态初始态（页面构造期只搭界面，下拉要等 `showEvent`
     才填，所以「打开就是空态」只能靠它）；
  ② `_sync_action_state()` 在每次数据刷新后按「当前有没有简历」重新同步。

只变异其中一处，「另一处」仍会把状态摆对，用例照样绿 —— 那不是判据强，
是**变异打偏了**（本轮就踩过一次：M1/M3 原本只砍 ②，而断言走的是构造期快照）。

做法：对 ``ui/pages/resume_optimize.py`` 做**精确字符串替换**（并校验锚点唯一）
→ 跑指定用例 → **无论结果都还原**。

⚠ 读写都必须 ``newline=""``：仓库约定 LF、``.gitattributes`` 是 ``* -text``、
打包产物按字节 sha256 校验。用默认文本模式回写，Python 在 Windows 会把
``\\n`` 翻成 ``os.linesep``，整个文件变成 CRLF，产物哈希跟着变
（G4 那次踩过：改 5 行的文件在 diff 里变成 798 行增删）。

运行：python tests/_mutate_g7_check.py
"""

import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
PKG = os.path.join(ROOT, "offerflow_desktop")
PAGE = os.path.join(PKG, "ui", "pages", "resume_optimize.py")
TEST = "test_resume_optimize_import.py"

# 空态引导有**两处**机制，各配一组变异（缺一个方向就会有假绿）：
#   * `_GUIDE`      —— `_sync_action_state()`：每次数据刷新后按「有没有简历」同步；
#   * `_CTOR_GUIDE` —— `__init__` 末尾摆的静态初始态：页面**构造期只搭界面**
#     （下拉是 `showEvent` 里 `QTimer.singleShot(0, _refresh)` 才填的），
#     所以「打开就是空态」这件事必须由构造期自己保证，不能指望那次刷新。
_GUIDE = ("        self.resume_hint.setVisible(not has_resume)\n"
          "        self.optimize_btn.setEnabled(has_resume)")
_CTOR_GUIDE = ("        self.resume_hint.setVisible(True)\n"
               "        self.optimize_btn.setEnabled(False)")
_IMPORT_BTN = "        self.import_btn = make_import_button(self, self._on_resume_imported)"

MUTATIONS = [
    {
        "id": "M1",
        "desc": "构造期引导条永不显示（缺简历时用户看不到任何可点的路）",
        "file": PAGE,
        # ⚠ 这里必须替换成 `setVisible(False)` 而不是**删掉整行**：Qt 控件默认
        # 就是可见的，删掉那一行引导条照样显形 —— 变异就变成了空靶。
        "before": _CTOR_GUIDE,
        "after": ("        self.resume_hint.setVisible(False)\n"
                  "        self.optimize_btn.setEnabled(False)"),
        "case": "TestEmptyGuidance.test_hint_shows_and_button_disabled_when_no_resume",
    },
    {
        "id": "M2",
        "desc": "引导条永不收起（有简历了还挂着「还没有简历」）",
        "file": PAGE,
        "before": _GUIDE,
        "after": ("        self.resume_hint.setVisible(True)\n"
                  "        self.optimize_btn.setEnabled(has_resume)"),
        "case": "TestEmptyGuidance.test_hint_hides_and_button_enabled_when_resume_exists",
    },
    {
        "id": "M3",
        "desc": "构造期主按钮恒可用（缺简历也能点，点了才弹错）",
        "file": PAGE,
        "before": _CTOR_GUIDE,
        "after": ("        self.resume_hint.setVisible(True)\n"
                  "        self.optimize_btn.setEnabled(True)"),
        "case": "TestEmptyGuidance.test_hint_shows_and_button_disabled_when_no_resume",
    },
    {
        "id": "M4",
        "desc": "_refresh 不再同步按钮状态（引导条不跟随数据刷新）",
        "file": PAGE,
        "before": ("        self._sync_action_state()\n"
                   "        # 版本列表走网络请求"),
        "after": "        # 版本列表走网络请求",
        "case": "TestEmptyGuidance.test_hint_tracks_the_data_both_ways",
    },
    {
        "id": "M4b",
        "desc": "_sync_action_state 不再同步主按钮（有简历也点不动，得重启页面）",
        "file": PAGE,
        # 锚点就是 M2 那两行的正体，这里只砍掉「同步按钮」那一半
        "before": _GUIDE,
        "after": "        self.resume_hint.setVisible(not has_resume)",
        "case": "TestEmptyGuidance.test_hint_hides_and_button_enabled_when_resume_exists",
    },
    {
        "id": "M5",
        "desc": "「去生成简历」跳错页",
        "file": PAGE,
        "before": 'self.gen_resume_btn.clicked.connect(lambda: self.mw.navigate("简历生成"))',
        "after": 'self.gen_resume_btn.clicked.connect(lambda: self.mw.navigate("简历优化"))',
        "case": "TestEntryWiring.test_generate_button_navigates_to_resume_generate",
    },
    {
        "id": "M6",
        "desc": "「去生成简历」改用主按钮样式（与「开始优化」抢视觉重心）",
        "file": PAGE,
        "before": '        set_prop(self.gen_resume_btn, "class", "ghost")',
        "after": '        set_prop(self.gen_resume_btn, "class", "primary")',
        "case": "TestEntryWiring.test_generate_button_is_a_ghost_so_it_does_not_outrank_start",
    },
    {
        "id": "M7",
        "desc": "「导入简历」的回调接成 no-op（弹得出框、导入后不选中）",
        "file": PAGE,
        "before": _IMPORT_BTN,
        "after": "        self.import_btn = make_import_button(self, lambda _id: None)",
        "case": "TestEntryWiring.test_import_button_runs_the_shared_flow_with_the_right_callback",
    },
    {
        "id": "M8",
        "desc": "有简历时把「导入简历」藏起来（想换一版的人只能先删旧的）",
        "file": PAGE,
        "before": _IMPORT_BTN,
        "after": _IMPORT_BTN + "\n        self.import_btn.setVisible(False)",
        "case": "TestEntryWiring.test_import_button_exists_even_when_a_resume_is_selected",
    },
    {
        "id": "M9",
        "desc": "导入成功后不选中新简历（停在原来那份上）",
        "file": PAGE,
        "before": "        self._refresh(select_resume=resume_id)",
        "after": "        self._refresh()",
        "case": "TestSelectionAfterImport.test_imported_resume_gets_selected",
    },
    {
        "id": "M10",
        "desc": "重填下拉丢掉「保留当前选择」（回滚后悄悄换成另一份）",
        "file": PAGE,
        "before": "        want = select_resume or self.resume_combo.currentData()",
        "after": "        want = select_resume",
        "case": "TestSelectionAfterImport.test_refresh_keeps_the_current_selection",
    },
    {
        "id": "M11",
        "desc": "select_resume 指向不存在的 id 时照样 setCurrentIndex（掉到空选择）",
        "file": PAGE,
        "before": ("            idx = self.resume_combo.findData(want)\n"
                   "            if idx >= 0:\n"
                   "                self.resume_combo.setCurrentIndex(idx)"),
        "after": ("            idx = self.resume_combo.findData(want)\n"
                  "            self.resume_combo.setCurrentIndex(idx)"),
        "case": "TestSelectionAfterImport.test_refresh_with_unknown_id_falls_back_instead_of_crashing",
    },
    {
        "id": "M12",
        "desc": "失败恢复写死 True（没简历时也被「恢复」成可点）",
        "file": PAGE,
        "before": ("        # 走统一入口而不是写死 True：按钮的可用性由「有没有简历」决定，\n"
                   "        # 这里硬开会在极端情况下（列表被清空）把一个空选择留在可点状态。\n"
                   "        self._sync_action_state()"),
        "after": "        self.optimize_btn.setEnabled(True)",
        "case": "TestSyncAfterRun.test_failed_run_keeps_the_button_disabled_without_a_resume",
    },
    {
        "id": "M13",
        "desc": "删掉「缺少选择」的兜底守卫（程序化点开时静默无反应）",
        "file": PAGE,
        "before": ('        if not resume_id or not jd_id:\n'
                   '            QMessageBox.warning(self, "缺少选择", "请先选择要优化的简历和目标岗位")\n'
                   '            return'),
        "after": "        if not resume_id or not jd_id:\n            return",
        "case": "TestEmptyGuidance.test_reverse_guard_still_exists_when_the_click_gets_through",
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
        # newline="" = 原样写出，不做任何行尾翻译（见文件头说明）
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
            if n > 1:
                print("[%s] 锚点在文件里出现 %d 次，变异落点不确定：%s" % (num, n, desc))
                bad.append("%s 锚点不唯一" % num)
                continue
            # 直接捕获基线子进程的真实 stdout/stderr/rc，避免事后重跑掩盖偶发崩溃
            _base = subprocess.run(
                [sys.executable, "-u", os.path.join(HERE, TEST), mut["case"]],
                cwd=ROOT, capture_output=True, text=True)
            if _base.returncode != 0:
                print("[%s] 基线就是红的，无法判断变异是否被拦住：%s" % (num, mut["case"]))
                print("        >>> rc=%s" % _base.returncode)
                print("        >>> stdout:\n" + "\n".join("        " + l for l in _base.stdout.splitlines()[:25]))
                print("        >>> stderr:\n" + "\n".join("        " + l for l in _base.stderr.splitlines()[:25]))
                bad.append("%s 基线红" % num)
                continue

            _write(mut["file"], original.replace(mut["before"], mut["after"]))
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
