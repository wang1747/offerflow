"""启动链路冒烟：把 ``main.py::_run_native`` 里"窗口引导"那一段真的跑一遍。

背景（2026-10-06 一次真实事故）：``ui/main_window.py`` 的 ``setup_tray()`` 里用了
``app_icon(32)``，但导航重构时把 ``app_icon`` 的 import 误删了 —— 小写名字，当时的
静态审计只查大写，没拦住。而**所有既有测试都只构造 ``MainWindow``，从不调用
``setup_tray``**（它唯一的调用点是 ``main.py:145``），于是：

    编译通过 → import 通过 → 全量 15 项测试全绿 → 打包通过 → 发版
    → 用户登录成功 → ``window.setup_tray()`` → NameError → **闪退**

覆盖不到的根因很具体：**构造 ≠ 初始化**。构造函数里没有的调用，构造测试就看不见。

本测试做两件事：
1. **漂移护栏**：用 AST 从 ``main.py`` 里抽出所有 ``window.<attr>`` 调用，断言这些
   成员在主窗口实例上真实存在 —— 将来改名/漏定义会在此处变红，而不是等用户点开。
2. **引导序列实跑**：复刻 ``main.py`` 的 构造 → setup_tray → bind_single_instance →
   主题图标重绘 → show，普通账号/管理员 × 浅色/深色共 4 组。

⚠️ 为什么 4 组要各起一个子进程（不是图省事，是被实测逼出来的）：
本平台（Windows + offscreen）实测，一个进程里**累积多个 show() 过的、又不能销毁的
主窗口**时，Qt 会在退出阶段触发 C 层 fast-fail —— 退出码 3221226505（0xC0000409），
没有任何 traceback，而 stdout 里所有判据都是通过；对 run_all 来说就是假红。逐项二分：

    show() 过 + os._exit() 直接退                 -> 0
    show() 过 + hide/close 后走解释器正常退出      -> 0xC0000409
    show() 过 + deleteLater + processEvents       -> 0xC0000409（os._exit 也救不回来）
    没有 show()                                   -> 0（所以 test_window_rebuild 一直好的）
    单个 show() 过的窗口 + hide/close + os._exit  -> 6/6 都是 0
    4 个累积窗口 + 同上                            -> 3 次里 2 次 0xC0000409

与 QSystemTrayIcon / QLocalServer 无关（2×2 开关矩阵四种组合结果一致）。
所以每个用例独立起进程：子进程里只有一个窗口，退出码确定；同时天然做到用例隔离。

刻意**不**复刻的两步（保持用例自洽、不引入网络副作用）：``_seed_local_index()``
与 ``sync_service.service().start()`` —— 与窗口引导无关，且会在测试进程里发真实请求。

运行：python tests/test_window_startup.py
"""

import ast
import os
import subprocess
import sys
import traceback

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, ROOT)

# 每组用例：(命令行键, 是否管理员, 主题, 展示名)
CASES = [
    ("user-light", False, "light", "普通账号/浅色"),
    ("admin-light", True, "light", "管理员/浅色"),
    ("user-dark", False, "dark", "普通账号/深色"),
    ("admin-dark", True, "dark", "管理员/深色"),
]

FAILURES = []


def check(ok: bool, label: str, detail: str = "") -> None:
    print(("  OK  " if ok else "  XX  ") + label + ("" if ok else "  -> %s" % detail))
    if not ok:
        FAILURES.append("%s（%s）" % (label, detail))


# ----------------------------------------------------------------------
# 子进程侧：单个用例的引导序列
# ----------------------------------------------------------------------
def run_one_case(role: bool, theme: str, label: str) -> None:
    """执行一组引导序列。函数返回前直接 os._exit（见模块 docstring 的退出码说明）。"""
    from PySide6.QtCore import QSize
    from PySide6.QtNetwork import QLocalServer
    from PySide6.QtWidgets import QApplication

    from offerflow_desktop import api_client as api
    from offerflow_desktop import database as db

    app = QApplication.instance() or QApplication([])
    db.init_db()
    api.is_admin_cached = (lambda r=role: r)

    from offerflow_desktop.ui import tokens
    from offerflow_desktop.ui.main_window import MainWindow

    tokens.set_theme(theme)

    server = QLocalServer()
    server_name = "OfferFlowStartupSmoke_%s" % label
    QLocalServer.removeServer(server_name)
    listened = server.listen(server_name)

    window = None
    try:
        window = MainWindow()                      # 构造
        window.setup_tray()                        # ← 事故点就在这一行
        window.bind_single_instance(server)        # 单实例 IPC
        window.reapply_icons(dark=(theme == "dark"))  # 主题图标重绘
        window.resize(QSize(1360, 920))
        window.show()                              # main.py 的最后一步
        app.processEvents()

        check(listened or server.isListening(),
              "[%s] 单实例 IPC 监听可用（前置条件）" % label,
              "listen 失败：%s" % server.errorString())
        check(getattr(window, "tray", None) is not None,
              "[%s] 系统托盘对象已建立" % label, "tray 未建立")
        check(window.nav.active_key == "首页",
              "[%s] 引导后侧栏高亮在首页" % label,
              "active=%r" % window.nav.active_key)
        check(window.isVisible(), "[%s] 主窗口已显示" % label)
    except Exception as exc:  # noqa: BLE001
        check(False, "[%s] 引导序列无异常" % label,
              "%s: %s\n%s" % (type(exc).__name__, exc, traceback.format_exc()))
    finally:
        try:
            if window is not None:
                # 刻意不调 deleteLater()：见 docstring，本平台上它会触发 C 层 abort。
                window.hide()
                window.close()
        except Exception:  # noqa: BLE001
            pass
        server.close()

    if FAILURES:
        print("[%s] 失败：%s" % (label, "；".join(FAILURES)))
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(1 if FAILURES else 0)


# ----------------------------------------------------------------------
# 父进程侧
# ----------------------------------------------------------------------
def _window_attrs_called_by_main() -> list:
    """从 main.py 里抽出 ``window.<attr>`` 形式的调用名（按 ``window = MainWindow()`` 绑定）。"""
    path = os.path.join(ROOT, "main.py")
    with open(path, encoding="utf-8") as fh:
        tree = ast.parse(fh.read(), path)

    bound = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call):
            func = node.value.func
            name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
            if name == "MainWindow":
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        bound.add(target.id)
    if not bound:
        return []

    attrs = set()
    for node in ast.walk(tree):
        if (isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Load)
                and isinstance(node.value, ast.Name) and node.value.id in bound):
            attrs.add(node.attr)
    return sorted(attrs)


def check_drift_guard() -> None:
    """护栏：main.py 调用的每个 window 成员都必须真实存在。

    刻意**只查类、不构造实例**：① main.py 调用的都是方法，`hasattr(cls, ...)`
    已经足够（还能覆盖从 QMainWindow 继承来的 ``show``）；② 父进程因此完全不碰
    Qt 对象 —— 本平台上"父进程里创建过窗口"会让退出码变成 0xC0000409（见 docstring），
    而父进程还要负责汇总 4 个子进程的结果，退出码不能是脏的。

    真正"调用得通"由子进程实跑覆盖（子进程会真的把这些方法都执行一遍）。
    """
    attrs = _window_attrs_called_by_main()
    check(bool(attrs), "从 main.py 抽到了窗口调用（前置条件）",
          "一个 window.* 都没解析到，护栏本身失效了")
    if not attrs:
        return

    from offerflow_desktop.ui.main_window import MainWindow

    missing = [a for a in attrs if not hasattr(MainWindow, a)]
    check(not missing,
          "main.py 调用的 %d 个窗口成员都在 MainWindow 上：%s" % (len(attrs), attrs),
          "缺失：%s" % missing)


def main() -> int:
    if "--case" in sys.argv:
        key = sys.argv[sys.argv.index("--case") + 1]
        for ckey, role, theme, label in CASES:
            if ckey == key:
                run_one_case(role, theme, label)
        raise SystemExit("未知用例：%s" % key)

    print("=== 1. main.py 的 window.<attr> 调用都要在主窗口上存在（漂移护栏）===")
    check_drift_guard()

    print("\n=== 2. 引导序列实跑（角色 × 主题，共 %d 组；每组独立子进程）===" % len(CASES))
    child_rc = {}
    for key, _role, _theme, label in CASES:
        print("\n  --- %s ---" % label)
        proc = subprocess.run([sys.executable, "-u", os.path.abspath(__file__),
                               "--case", key])
        child_rc[key] = proc.returncode
        if proc.returncode != 0:
            FAILURES.append("[%s] 子进程退出码 %d" % (label, proc.returncode))

    if FAILURES:
        print("\n启动链路冒烟失败：%d 项" % len(FAILURES))
        for item in FAILURES:
            print("  - " + item)
    else:
        print("\n启动链路冒烟全部通过（%d 组引导 + 漂移护栏）" % len(CASES))

    # 退出码自己钉死（父进程不创建任何 Qt 对象，这里只是让退出码完全可控）。
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(1 if FAILURES else 0)


if __name__ == "__main__":
    sys.exit(main())
