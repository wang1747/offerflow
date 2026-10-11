"""侧栏导航注册与渲染测试。

覆盖导航重设计（v2.2.24）：
- 注册表是唯一事实来源：键唯一、每条都有副标题、分组收敛
- 角色门控：非管理员看不到「管理后台」；管理员的「管理」分组可运行期补上（幂等）
- **启动落地页必须高亮**（旧实现落在不可选的分组标题上，16/16 项都不高亮）
- 点击 / ``navigate()`` 两条路径「切页 + 高亮」必须同时发生
- 侧栏底部用户卡显示昵称（无昵称回退邮箱）与角色

运行：python tests/test_nav_registration.py
"""

import gc
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, os.path.abspath(ROOT))

from PySide6.QtWidgets import QApplication                      # noqa: E402

from offerflow_desktop import api_client as api                  # noqa: E402
from offerflow_desktop import database as db                     # noqa: E402
from offerflow_desktop.ui import icons                           # noqa: E402
from offerflow_desktop.ui import nav_registry as navreg           # noqa: E402
from offerflow_desktop.ui.main_window import MainWindow          # noqa: E402

COST = "AI 用量与成本"
ADMIN = "管理后台"
HOME = "首页"
AGENT = "求职 Agent"

fails = []


def check(cond, label, extra=""):
    print(("OK  " if cond else "XX  ") + label + ("  " + str(extra) if extra else ""))
    if not cond:
        fails.append(label)


def make_window(role_cached):
    api.is_admin_cached = (lambda r=role_cached: r)
    return MainWindow()


def dispose(app, win):
    win.close()
    win.deleteLater()
    app.processEvents()
    gc.collect()


def main() -> int:
    app = QApplication.instance() or QApplication([])
    db.init_db()

    # ===== 1. 注册表自洽 =====
    print("A. 注册表")
    items = navreg.all_items()
    keys = [i.key for i in items]
    check(len(keys) == len(set(keys)), "页面键唯一", "%d 条" % len(keys))
    check(all(i.subtitle.strip() for i in items), "每个条目都有副标题")
    check(all(i.label.strip() for i in items), "每个条目都有主标题")
    check(all(i.icon for i in items), "每个条目都指定了图标")
    check(all(i.icon in icons._DRAWERS for i in items),
          "图标名都在 icons 里存在",
          str([i.icon for i in items if i.icon not in icons._DRAWERS]))
    check(navreg.home_item() is not None, "注册表定义了首页",
          navreg.home_item().key if navreg.home_item() else "")
    check(navreg.item_by_key("不存在的键") is None, "按未知键查条目返回 None")

    # 三个分组 + 一个管理分组；管理分组只有一个条目
    check(len(navreg.NAV_GROUPS) == 4, "注册表共 4 个分组",
          str([g.label for g in navreg.NAV_GROUPS]))
    admin_items = [i for i in items if i.admin_only]
    check(len(admin_items) == 1 and admin_items[0].key == ADMIN,
          "只有「管理后台」标记为仅管理员")

    # 非管理员的分组过滤：空分组必须消失（否则会留下光秃秃的「管理」标题）
    user_groups = navreg.groups_for(is_admin=False)
    check(all(g.items for g in user_groups), "非管理员时没有空分组")
    check("管理" not in [g.label for g in user_groups],
          "非管理员看不到「管理」分组")
    check(len(navreg.iter_items(is_admin=False)) == len(items) - 1,
          "非管理员可见条目 = 全部 - 1")
    check(len(navreg.iter_items(is_admin=None)) == len(items) - 1,
          "角色未知时同样不显示管理入口")

    # 主线能力必须在前排：「求职 Agent」此前排在工作区最后一位
    work = [g for g in navreg.NAV_GROUPS if g.key == "work"][0]
    check(work.items[1].key == AGENT,
          "「求职 Agent」位于工作区第 2 位（不再被埋在最后）",
          str([i.key for i in work.items]))

    # ===== 2. 非管理员：侧栏构造 =====
    print("\nB. 非管理员")
    w = make_window(False)
    check(ADMIN not in w.pages, "非管理员不显示管理后台")
    check(COST in w.pages, "非管理员也能看 AI 用量与成本")
    check(w.nav.row_count == 13, "侧栏 13 个可点击条目", w.nav.row_count)
    check(len(w.pages) == 13 and w.stack.count() == 13,
          "页面栈与 pages 字典都是 13",
          "stack=%d pages=%d" % (w.stack.count(), len(w.pages)))
    check(w.nav.active_key == HOME,
          "启动即高亮首页（旧实现 16/16 项都不高亮）", w.nav.active_key)
    check(w.stack.currentWidget() is w.pages[HOME], "启动显示首页")
    dispose(app, w)

    # ===== 3. 管理员：侧栏构造 =====
    print("\nC. 管理员")
    w = make_window(True)
    check(ADMIN in w.pages, "管理员显示管理后台")
    check(w.nav.row_count == 14, "侧栏 14 个可点击条目", w.nav.row_count)
    keys_in_order = list(w.nav.keys())
    check(keys_in_order[-1] == ADMIN, "管理后台位于侧栏最后一项", keys_in_order[-1])
    check(keys_in_order.index(COST) < keys_in_order.index("模型设置")
          < keys_in_order.index("本地知识库") < keys_in_order.index("系统设置"),
          "系统分组顺序：AI 用量与成本 → 模型设置 → 本地知识库 → 系统设置")
    check(len(keys_in_order) == len(set(keys_in_order)), "侧栏没有重复项")
    check(w.nav.active_key == HOME, "管理员启动同样高亮首页")
    dispose(app, w)

    # ===== 4. 运行期补查角色：动态追加，且幂等 =====
    print("\nD. 角色补查")
    w = make_window(None)          # 老登录态：本机没缓存角色
    check(ADMIN not in w.pages, "角色未知时不显示（不阻塞启动）")
    api.is_admin_cached = (lambda: True)
    w._maybe_add_admin_page()
    first = w.nav.row_count
    w._maybe_add_admin_page()
    w._maybe_add_admin_page()
    check(ADMIN in w.pages, "补查后追加管理后台")
    check(w.nav.row_count == first, "重复调用不重复追加（幂等）", w.nav.row_count)
    check(w.stack.count() == len(w.pages), "页面栈与页面字典数量一致",
          "stack=%d pages=%d" % (w.stack.count(), len(w.pages)))
    # 补查不得把用户从当前页弹走
    check(w.nav.active_key == HOME, "补查后当前页保持不变（不弹回首页）",
          w.nav.active_key)

    # ===== 5. 跳转：切页与高亮必须同时发生 =====
    print("\nE. 跳转")
    check(w.navigate(COST) is True, "navigate 命中返回 True")
    check(w.stack.currentWidget() is w.pages[COST], "navigate 切到成本页")
    check(w.nav.active_key == COST, "navigate 同步侧栏高亮", w.nav.active_key)
    check(w.navigate(ADMIN) is True, "navigate 能切到管理后台")
    check(w.nav.active_key == ADMIN, "navigate 同步高亮管理后台")
    w.navigate(COST)
    check(w.navigate("不存在的页面") is False, "未知页面键返回 False（不报错）")
    check(w.stack.currentWidget() is w.pages[COST], "未知页面键不改变当前页")

    # ===== 6. 侧栏用户卡（身份 + 角色 + 昵称回退） =====
    print("\nF. 侧栏用户卡")
    saved = api._state.get("user")
    api._state["user"] = {"email": "u@example.com", "name": "小明", "role": "user"}
    w.refresh_status()
    check(w.user_card.name.text() == "小明", "用户卡显示昵称", w.user_card.name.text())
    check(w.user_card.role.text() == "成员", "用户卡显示角色「成员」",
          w.user_card.role.text())
    check(w.user_card.avatar.text() == "小", "头像取昵称首字（不依赖图片加载）",
          w.user_card.avatar.text())
    api._state["user"] = {"email": "u@example.com", "name": "", "role": "admin"}
    w.refresh_status()
    check("u@example.com" in w.user_card.name.text(), "无昵称时回退邮箱",
          w.user_card.name.text())
    check(w.user_card.role.text() == "管理员", "角色切换到管理员",
          w.user_card.role.text())
    api._state["user"] = saved
    dispose(app, w)

    print()
    if fails:
        print("FAILED: %d 处 -> %s" % (len(fails), "; ".join(fails)))
        return 1
    print("ALL OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
