"""主窗口反复构造 / 销毁的稳定性测试。

背景：v2.2.7 发现一个致命缺陷 —— 管理后台的 Tab 在构造期发网络请求，
导致**第二次构造主窗口时进程直接崩溃**（无 Python 异常、无 traceback）。
真实场景就是「退出登录 → 再登录」，用户一定会撞上。

这个测试用「反复构造 + 销毁主窗口」复现该场景：只要有任何页面在构造期
起后台线程并持有已销毁控件的回调，这里就会崩，从而在打包前拦住问题。

注意：**不调用 show()** —— show 会触发 showEvent 里的正常网络加载，
测试只关心「构造 + 销毁」这条路径。

运行：python tests/test_window_rebuild.py
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
from offerflow_desktop.ui.main_window import MainWindow          # noqa: E402

# (角色缓存值, 期望侧栏条目数)：True/False 是已缓存角色，None 表示本机没缓存。
# 条目数**不含分组标题** —— 分组不是导航项（不可点、不参与选中），
# 所以非管理员 13 条（3 组）、管理员 14 条（4 组）。
ROUNDS = [
    (True, 14),
    (True, 14),
    (None, 13),
    (False, 13),
    (True, 14),
]


def main() -> int:
    app = QApplication.instance() or QApplication([])
    db.init_db()

    failures = []
    for i, (role, expect) in enumerate(ROUNDS, 1):
        api.is_admin_cached = (lambda r=role: r)
        try:
            win = MainWindow()
        except Exception as exc:                  # noqa: BLE001
            failures.append("第 %d 次构造抛异常 %s: %s" % (i, type(exc).__name__, exc))
            print("XX  第 %d 次构造 role=%s -> %s: %s"
                  % (i, role, type(exc).__name__, exc))
            continue

        n_nav = win.nav.row_count
        n_stack = win.stack.count()
        n_pages = len(win.pages)
        # 重建后「启动高亮」也必须回来 —— 否则第二次登录进来侧栏是空的
        # （旧实现就是坏的：落在不可选的分组标题上，一条都不高亮）
        active = win.nav.active_key
        ok = n_nav == expect and n_stack == n_pages and active == "首页"
        print(("OK  " if ok else "XX  ")
              + "第 %d 次构造 role=%-5s 侧栏=%d 页面栈=%d 字典=%d 高亮=%s"
              % (i, role, n_nav, n_stack, n_pages, active))
        if not ok:
            failures.append(
                "第 %d 次构造结构不对：nav=%d stack=%d pages=%d 高亮=%r（期望 %d / 首页）"
                % (i, n_nav, n_stack, n_pages, active, expect))

        win.close()
        win.deleteLater()
        app.processEvents()
        gc.collect()

    print()
    if failures:
        print("FAILED: %d 处" % len(failures))
        for item in failures:
            print("  -", item)
        return 1
    print("ALL OK（主窗口连续构造/销毁 %d 次，无崩溃）" % len(ROUNDS))
    return 0


if __name__ == "__main__":
    sys.exit(main())
