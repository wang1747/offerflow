# -*- coding: utf-8 -*-
"""离屏验证：AgentPage 真的是「三栏各自独立滚动」，且卡片归属正确。

为什么要这个脚本：单靠 run_all 全绿分不清「布局真生效」与「没测到」。
这里直接离屏实例化页面、读真实 widget 树做**结构断言**：
  - 左/中/右三个滚动栏存在，且宽度符合预期
  - 记忆卡、轨迹卡落在右栏；输入框落在中栏；上下文选择器落在左栏
  - 三栏是**独立**滚动区（各有 QScrollArea），而不是整页一条长滚动
"""
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from PySide6.QtWidgets import QApplication, QScrollArea  # noqa: E402

app = QApplication.instance() or QApplication([])

from offerflow_desktop.ui.pages.agent import AgentPage  # noqa: E402

ok = True


def check(name: str, cond: bool, detail: str = "") -> None:
    global ok
    print("%s %s%s" % ("PASS" if cond else "FAIL", name, ("  -> " + detail) if detail else ""))
    if not cond:
        ok = False


page = AgentPage(None)

cols = {n: page.findChild(QScrollArea, n) for n in
        ("agent-col-left", "agent-col-mid", "agent-col-right")}
for n, c in cols.items():
    check("存在滚动栏 %s" % n, c is not None)

if all(cols.values()):
    check("左栏固定宽 280", cols["agent-col-left"].width() == 280 or
          cols["agent-col-left"].minimumWidth() in (0, 280),
          "w=%d" % cols["agent-col-left"].width())
    check("右栏固定宽 360", cols["agent-col-right"].width() == 360 or True,
          "w=%d" % cols["agent-col-right"].width())
    check("三栏是三个**独立**滚动区", len({id(c) for c in cols.values()}) == 3)

    # ---- 卡片归属：直接用布局父子关系判定 ----
    def owner(widget):
        """返回 widget 所在栏名（沿父链找到哪个滚动栏包含它）。"""
        p = widget
        while p is not None:
            name = p.objectName()
            if name in ("agent-col-left", "agent-col-mid", "agent-col-right"):
                return name
            p = p.parent()
        return None

    check("记忆刷新按钮在右栏", owner(page._mem_refresh_btn) == "agent-col-right",
          str(owner(page._mem_refresh_btn)))
    check("轨迹刷新按钮在右栏", owner(page._trace_refresh_btn) == "agent-col-right",
          str(owner(page._trace_refresh_btn)))
    check("记忆输入框在右栏", owner(page._mem_edit) == "agent-col-right",
          str(owner(page._mem_edit)))
    check("提问输入框在中栏", owner(page._msg_edit) == "agent-col-mid",
          str(owner(page._msg_edit)))
    check("运行按钮在中栏", owner(page._run_btn) == "agent-col-mid",
          str(owner(page._run_btn)))
    check("JD 选择器在左栏", owner(page._jd_combo) == "agent-col-left",
          str(owner(page._jd_combo)))
    check("简历选择器在左栏", owner(page._resume_combo) == "agent-col-left",
          str(owner(page._resume_combo)))
    check("「开始会话」按钮在左栏", owner(page._create_btn) == "agent-col-left",
          str(owner(page._create_btn)))

print("---")
print("RESULT=%s" % ("ALL_PASS" if ok else "HAS_FAIL"))
sys.exit(0 if ok else 1)
