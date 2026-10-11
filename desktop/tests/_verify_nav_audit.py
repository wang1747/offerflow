# -*- coding: utf-8 -*-
"""侧栏导航体检：把「导航好不好用」变成可测量的数字。

为什么要它：导航问题（末尾项点不到、启动没高亮、条目区分不开）
单看截图说不清是「设计取舍」还是「真的坏了」。这里离屏实例化主窗口，
在多种窗口尺寸下读**真实 widget 几何**，把两类问题分开：

  A. 结构性问题（与窗口大小无关，必须为 0）
     - 启动落地页是否真的高亮（不是「页面显示了但侧栏没反应」）
     - 每个条目是否都有副标题（区分相近模块的唯一手段）
     - 分组数是否收敛
     - 点击 → 切页 + 高亮 是否同时发生

  B. 溢出可达性（窗口变小时允许滚动，但**不允许够不到**）
     - 内容溢出时滚动条必须可用（原先 ScrollBarAlwaysOff ⇒ 末尾项物理不可达）
     - 滚到底之后，最后一项必须完全可见

判据全部基于几何与数据，不依赖目视。
"""
import gc
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from PySide6.QtCore import QSize, Qt                                     # noqa: E402
from PySide6.QtTest import QTest                                         # noqa: E402
from PySide6.QtWidgets import QApplication                               # noqa: E402

from offerflow_desktop import api_client as api                          # noqa: E402
from offerflow_desktop import database as db                             # noqa: E402
from offerflow_desktop.ui import nav_registry as navreg                  # noqa: E402
from offerflow_desktop.ui.main_window import MainWindow                  # noqa: E402

app = QApplication.instance() or QApplication([])
db.init_db()

SIZES = [("最小", QSize(1120, 740)), ("默认", QSize(1360, 920)), ("大屏", QSize(1600, 1000))]
fails = []


def check(cond, label, extra=""):
    print(("  OK  " if cond else "  XX  ") + label + ("   " + str(extra) if extra else ""))
    if not cond:
        fails.append(label)


def audit(role, role_label):
    api.is_admin_cached = (lambda r=role: r)
    win = MainWindow()
    win.show()          # 必须 show：隐藏状态下父布局不会真正激活，
                        # 子控件几何停留在构造时的初始值（各尺寸量出来一模一样）
    app.processEvents()
    nav = win.nav
    print("\n===== %s =====" % role_label)

    # ---------- A. 结构性问题 ----------
    home = navreg.home_item()
    check(nav.active_key == home.key,
          "启动落地页高亮（%s）" % home.key, "active=%r" % nav.active_key)
    check(win.stack.currentWidget() is win.pages[home.key],
          "启动落地页显示正确", win.stack.currentWidget().__class__.__name__)

    rows = [nav.row(k) for k in nav.keys()]
    check(all(r is not None for r in rows), "每个 nav item 都有对应的行控件")
    missing = [r.key for r in rows if not r.sub.text().strip()]
    check(not missing, "每个条目都有副标题（区分相近模块）", "缺失=%s" % missing)

    group_labels = list(nav.group_labels())
    max_groups = 4 if role else 3      # 管理员多一个独立的「管理」上下文
    check(len(group_labels) <= max_groups,
          "分组不超过 %d 个" % max_groups,
          "实为 %d：%s" % (len(group_labels), group_labels))
    check(len(group_labels) == len(set(group_labels)), "分组标题不重复")

    # 分组标题不是导航项（不可点）：它不参与 keys()
    check(all(g not in nav.keys() for g in group_labels),
          "分组标题不在可点击条目集合里")

    # ---------- 点击 → 切页 + 高亮 ----------
    target = "系统设置"
    if target not in nav.keys():
        target = nav.keys()[-1]
    nav.row(target).activated.emit(target)
    app.processEvents()
    check(win.stack.currentWidget() is win.pages[target],
          "点击条目切到对应页面", target)
    check(nav.active_key == target, "点击条目同步高亮", "active=%r" % nav.active_key)
    only_one = [k for k in nav.keys() if nav.row(k).is_active]
    check(only_one == [target], "同时只有一条处于高亮态", only_one)

    # ---------- 真实鼠标点击（不是直接 emit 信号）----------
    # 必要性：条目里的文字/图标是 QLabel 子控件，鼠标事件要先被子控件接住、
    # 忽略后才冒泡到行控件。只测信号等于跳过了这一整条链路 ——
    # 「信号连着」和「鼠标点得动」是两件事。
    click_target = "面试准备"
    if click_target in nav.keys():
        row = nav.row(click_target)
        QTest.mouseClick(row, Qt.MouseButton.LeftButton,
                         pos=row.rect().center())
        app.processEvents()
        check(nav.active_key == click_target, "真实鼠标点击能切换高亮",
              "active=%r" % nav.active_key)
        check(win.stack.currentWidget() is win.pages[click_target],
              "真实鼠标点击能切换页面")
        # 点在文字子控件上也要有效（用户点的就是文字）
        other = "薪资谈判"
        row2 = nav.row(other)
        QTest.mouseClick(row2.title, Qt.MouseButton.LeftButton,
                         pos=row2.title.rect().center())
        app.processEvents()
        check(nav.active_key == other, "点在文字上同样有效（事件冒泡到位）",
              "active=%r" % nav.active_key)

    # navigate 走同一路径
    win.navigate(home.key)
    check(nav.active_key == home.key, "navigate 同步高亮", "active=%r" % nav.active_key)
    check(win.navigate("不存在的页面") is False, "未注册的 key 返回 False（不抛错）")

    # ---------- B. 溢出可达性 ----------
    sb = nav.verticalScrollBar()
    for size_label, size in SIZES:
        win.resize(size)
        app.processEvents()
        win.resize(size)
        app.processEvents()

        viewport_h = nav.viewport().height()
        body = nav.widget()
        content_h = body.sizeHint().height()
        need_h = body.minimumSizeHint().height()
        overflow = need_h > viewport_h

        # 滚到底，再看最后一项是否完整可见
        sb.setValue(sb.maximum())
        app.processEvents()
        last = nav.row(nav.keys()[-1])
        top = last.mapTo(nav.viewport(), last.rect().topLeft()).y()
        bottom = top + last.height()
        visible = top >= -1 and bottom <= viewport_h + 1

        print("\n  [%s %dx%d]  win.h=%d nav.h=%d viewport=%d need=%d hint=%d scroll.max=%d"
              % (size_label, size.width(), size.height(),
                 win.height(), nav.height(), viewport_h, need_h, content_h, sb.maximum()))
        check(not overflow or sb.maximum() > 0,
              "溢出时滚动条可用（%s）" % size_label,
              "需要=%d 可视=%d 溢出=%s scroll.max=%d"
              % (need_h, viewport_h, overflow, sb.maximum()))
        # 不溢出时不应残留滚动条（会白占 6px 且看起来像「下面还有内容」）
        check(overflow or not sb.isVisible(),
              "不溢出时不显示滚动条（%s）" % size_label,
              "溢出=%s 可见=%s" % (overflow, sb.isVisible()))
        check(visible,
              "滚到底后最后一项「%s」完整可见（%s）" % (nav.keys()[-1], size_label),
              "top=%d bottom=%d viewport=%d" % (top, bottom, viewport_h))
        if overflow:
            check(nav.verticalScrollBarPolicy()
                  == Qt.ScrollBarPolicy.ScrollBarAsNeeded,
                  "滚动策略为按需显示（%s）" % size_label,
                  str(nav.verticalScrollBarPolicy()))

    win.hide()
    win.close()
    win.deleteLater()
    app.processEvents()
    gc.collect()


audit(False, "普通账号")
audit(True, "管理员")

print()
if fails:
    print("发现 %d 处问题：" % len(fails))
    for f in fails:
        print("  - " + f)
    sys.exit(1)
print("导航体检全部通过")
