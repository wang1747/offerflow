#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""看板拖拽判据的**反向对照**：把缺陷放回去，看用例是否真的变红。

为什么必须有这一步：一条全绿的用例只说明「当前代码能过它」，
不说明「换个错实现它会拦下来」。判据写歪了（取错字段、断言方向写反、
压根没覆盖到那条分支）时，用例照样全绿 —— 这种假绿比没写用例更危险。

做法：对 ``ui/pages/tracker.py`` 做精确字符串替换 → 跑指定用例 →
**无论结果都还原**（还原失败直接抛，避免把变异留在工作区）。

⚠ 读写都必须 ``newline=""``（关闭行尾转换）。仓库约定是 LF，且
``.gitattributes`` 是 ``* -text``（git 不做归一化）、打包产物按文件字节
sha256 校验；用默认文本模式读回写，Python 在 Windows 上会把 ``\n``
翻译成 ``os.linesep``，于是**整个文件被翻成 CRLF** ——
`git diff` 直接变成「整文件重写」，而且污染产物哈希校验。
（第一次写这个脚本就踩了：改 5 行的文件变成 798 行增删。）

运行：python tests/_mutate_kanban_check.py
"""

import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
PKG = os.path.join(ROOT, "offerflow_desktop")
TRACKER = os.path.join(PKG, "ui", "pages", "tracker.py")
THEME = os.path.join(PKG, "ui", "theme.py")

# (编号, 说明, 目标文件, 原文, 变异后, 期望变红的用例)
MUTATIONS = [
    (
        "M1",
        "去掉「拖回原列 = no-op」",
        TRACKER,
        """        app = next((a for a in self._apps if a.get("id") == app_id), None)
        if app is None or app.get("status") == new_status:
            return False""",
        """        app = next((a for a in self._apps if a.get("id") == app_id), None)
        if app is None:
            return False""",
        "TestDragAndDrop.test_reverse_drop_on_same_column_is_a_noop",
    ),
    (
        "M2",
        "去掉落点状态枚举校验",
        TRACKER,
        """        if new_status not in APPLICATION_STATUSES:
            return False
""",
        "",
        "TestDragAndDrop.test_reverse_unknown_status_is_refused_locally",
    ),
    (
        "M3",
        "dropEvent 不解析载荷、交给 Qt 默认实现",
        TRACKER,
        """            raw = bytes(event.mimeData().data(KANBAN_MIME))
            app_id = raw.decode("utf-8", "ignore")
            if app_id:
                self._on_drop(app_id, self._status)
            return""",
        """            return""",
        "TestDragAndDrop.test_drop_changes_status_and_persists",
    ),
    (
        "M4",
        "空列改用占位条目（幽灵卡片）",
        TRACKER,
        """        empty = hint_label("暂无投递", shrink=True)""",
        """        empty = hint_label("暂无投递", shrink=True)
        if not cards:
            column_list.addItem("暂无投递")""",
        "TestBoardLayout.test_reverse_empty_column_has_no_phantom_card",
    ),
    (
        "M5",
        "startDrag 不塞载荷（拖拽有动作、无数据）",
        TRACKER,
        """        mime = QMimeData()
        mime.setData(KANBAN_MIME, app_id.encode("utf-8"))""",
        """        mime = QMimeData()""",
        "TestDragAndDrop.test_drag_payload_carries_app_id",
    ),
    (
        "M6",
        "浅色主题漏掉卡片条目样式（卡片与列底糊成一片）",
        THEME,
        """QListWidget[class="kanban-list"]::item {
    background: #ffffff; border: 1px solid #e2e8f0; border-radius: 6px;
    padding: 6px 8px; color: #1f2937;
}""",
        "",
        "TestBoardTheme.test_column_and_card_colors_follow_the_theme",
    ),
    (
        "M7",
        "落点高亮选择器写错属性值（Qt 静默忽略）",
        THEME,
        """QListWidget[class="kanban-list"][dragover="true"] {
    background: rgba(79, 70, 229, 0.10); border-radius: 8px;
}""",
        """QListWidget[class="kanban-list"][dragover="yes"] {
    background: rgba(79, 70, 229, 0.10); border-radius: 8px;
}""",
        "TestBoardTheme.test_dragover_highlight_shows_and_clears",
    ),
    (
        "M8",
        "看板列不再挂样式类（列容器与列表都退回默认外观）",
        TRACKER,
        """        self.setProperty("class", "kanban-list")""",
        """        self.setProperty("class", "list")""",
        "TestBoardTheme.test_column_and_card_colors_follow_the_theme",
    ),
]


def run_case(case: str) -> bool:
    """跑单个用例，返回是否通过。"""
    proc = subprocess.run(
        [sys.executable, "-u", os.path.join(HERE, "test_tracker_kanban.py"), case],
        cwd=ROOT, capture_output=True, text=True)
    return proc.returncode == 0


def main() -> int:
    sources = {}
    for path in {m[2] for m in MUTATIONS}:
        # newline="" = 原样读写，不做任何行尾翻译（见文件头说明）
        with open(path, encoding="utf-8", newline="") as fh:
            sources[path] = fh.read()

    def _write(path: str, text: str) -> None:
        with open(path, "w", encoding="utf-8", newline="") as fh:
            fh.write(text)

    bad = []
    try:
        # 先确认基线：不动代码时这条用例是绿的，否则下面的「变红」无从谈起
        for num, desc, path, before, after, case in MUTATIONS:
            original = sources[path]
            if before not in original:
                print("[%s] 锚点没匹配上，变异无效：%s" % (num, desc))
                bad.append("%s 锚点失配" % num)
                continue
            if not run_case(case):
                print("[%s] 基线就是红的，无法判断变异是否被拦住：%s" % (num, case))
                bad.append("%s 基线红" % num)
                continue

            _write(path, original.replace(before, after, 1))
            try:
                passed = run_case(case)
            finally:
                _write(path, original)

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
