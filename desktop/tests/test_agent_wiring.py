"""Agent 模块回归测试（审计 #316 / #317 / #318）。

覆盖三件事：
1. P0：AgentPage 构造期不得启动后台线程（这是 logout→relogin 重建 MainWindow
   时旧页面回调访问已释放 Qt 对象、以 0xC0000409 静默崩进程的根因）。
2. P1-2：JD / 简历优化 / 投递追踪三页的「用 Agent 制定行动」必须调用
   mw.open_agent()，并传入正确的上下文 id（jd_id / resume_id / application_id）。
3. 主窗口重建（模拟退出登录再登录）不崩，且 AgentPage 销毁后回调被守卫拦下。

运行：python tests/test_agent_wiring.py
"""

import gc
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, os.path.abspath(ROOT))

from PySide6.QtWidgets import QApplication, QWidget            # noqa: E402
from PySide6.QtGui import QShowEvent                           # noqa: E402
from PySide6.QtCore import QEvent                             # noqa: E402
from shiboken6 import isValid                                  # noqa: E402

app = QApplication.instance() or QApplication([])


class FakeMW(QWidget):
    """记录 open_agent 调用，并提供页面构造时可能回调的宿主接口。"""

    def __init__(self):
        super().__init__()
        self.agent_calls = []

    def open_agent(self, entry):
        self.agent_calls.append(entry)

    def refresh_status(self):
        pass

    def refresh_ai_source(self):
        pass

    def navigate(self, *a, **k):
        pass

    def set_status(self, *a, **k):
        pass

    def reapply_icons(self, *a, **k):
        pass


fails = []


def check(cond, label, extra=""):
    print(("OK  " if cond else "XX  ") + label + ("  " + str(extra) if extra else ""))
    if not cond:
        fails.append(label)


def main() -> int:
    from offerflow_desktop.ui.pages.agent import AgentPage
    import offerflow_desktop.ui.pages.agent as _agent_mod
    from PySide6.QtWidgets import QMessageBox

    # headless 下 QMessageBox.* 是模态 exec()，会永久阻塞等用户点击。测试中把
    # 它们替换成不阻塞的 no-op（仅返回 Ok），避免「无选中项」等分支卡死进程。
    QMessageBox.information = staticmethod(lambda *a, **k: QMessageBox.StandardButton.Ok)
    QMessageBox.warning = staticmethod(lambda *a, **k: QMessageBox.StandardButton.Ok)
    QMessageBox.critical = staticmethod(lambda *a, **k: QMessageBox.StandardButton.Ok)

    # 离线桩：让上下文加载的后台任务瞬间返回，避免真实联网（沙箱会拦截/
    # 挂起）。这仍能验证「构造期不启动线程」「showEvent 才启动」「销毁守卫」。
    _agent_mod.api.list_jds = lambda: []
    _agent_mod.api.list_resumes = lambda: []
    _agent_mod.api.list_applications = lambda: []

    # ===== 1. 构造期不得启动后台线程（P0 根因回归） =====
    page = AgentPage(FakeMW())
    check(page._workers == [], "AgentPage 构造期不启动后台线程", page._workers)
    check(page._contexts_loaded is False, "构造期不置 _contexts_loaded")

    # 首次 showEvent 才触发一次加载，重复调用不重复加载
    page.showEvent(QShowEvent())
    check(len(page._workers) >= 1, "showEvent 触发上下文加载", len(page._workers))
    n = len(page._workers)
    page.showEvent(QShowEvent())
    check(len(page._workers) == n, "showEvent 重复调用不重复加载")

    # 销毁守卫：页面删除后再处理事件，回调不得崩进程
    page.deleteLater()
    app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    app.processEvents()
    check(not isValid(page), "页面已销毁，回调守卫可安全早退")

    # 排干第一部分 showEvent 启动的后台 LLMWorker，避免残留线程把进程拖住
    # （LLMWorker 由模块级 _ALIVE 强引用保活，是普通 QThread，未结束时 Python
    # 不会退出，看起来像卡死）。桩已让 fn 瞬间返回，这里轮询直到 _ALIVE 清空。
    from offerflow_desktop.llm import _ALIVE
    for _ in range(200):
        app.processEvents()
        if not _ALIVE:
            break

    # ===== 2. 三页接线（P1-2） =====
    from offerflow_desktop.ui.pages.jd_analysis import JDAnalysisPage
    from offerflow_desktop.ui.pages.resume_optimize import ResumeOptimizePage
    from offerflow_desktop.ui.pages.tracker import TrackerPage

    # 2a) JD 页
    mw = FakeMW()
    jd = JDAnalysisPage(mw)
    jd._selected_jd = lambda: {"id": "jd-123", "company": "A", "position": "B"}
    jd._open_agent_for_jd()
    check(
        any(c.get("jd_id") == "jd-123" and c.get("entry_source") == "jd_analysis"
            for c in mw.agent_calls),
        "JD 页「用 Agent 制定行动」传入 jd_id", mw.agent_calls)

    # 2b) 简历优化页
    mw = FakeMW()
    ro = ResumeOptimizePage(mw)
    ro._resume = {"id": "r-1"}
    ro._jd = {"id": "j-1"}
    ro._open_agent()
    check(
        any(c.get("resume_id") == "r-1" and c.get("jd_id") == "j-1"
            and c.get("entry_source") == "resume_optimize" for c in mw.agent_calls),
        "简历优化页传入 resume_id + jd_id", mw.agent_calls)

    # 2c) 投递追踪页
    mw = FakeMW()
    tr = TrackerPage(mw)
    tr._selected_app = lambda: {"id": "a-1", "jd_id": "jx", "resume_id": "rx"}
    tr._open_agent_for_app()
    check(
        any(c.get("application_id") == "a-1" and c.get("jd_id") == "jx"
            and c.get("resume_id") == "rx" for c in mw.agent_calls),
        "投递追踪页传入 application_id + jd_id + resume_id", mw.agent_calls)

    # 2d) 无选中项时只提示、不调用 open_agent
    mw = FakeMW()
    tr2 = TrackerPage(mw)
    tr2._selected_app = lambda: None
    tr2._open_agent_for_app()
    check(mw.agent_calls == [], "无选中项时不调用 open_agent")

    # ===== 3. 主窗口重建（退出登录再登录）不崩 =====
    # 注：不调用 show()，避免触发 showEvent 里的后台联网（profile / update /
    # ai_source），让本测试在离线环境也能秒级跑完且确定。P0 守卫已在第 1 节
    # 用 AgentPage.showEvent + deleteLater 覆盖。这里只验证「构造 + 销毁 +
    # 再次构造」这条 relogin 路径本身不会崩。
    from offerflow_desktop import database as db
    from offerflow_desktop import api_client as api
    from offerflow_desktop.ui.main_window import MainWindow
    print("[p3] imports done", flush=True)
    db.init_db()
    api.is_admin_cached = lambda: False   # 避免构造期联网（同 nav 测试的做法）
    print("[p3] init_db done", flush=True)
    try:
        print("[p3] construct w1", flush=True)
        w = MainWindow()
        print("[p3] w1 done", flush=True)
        w.logout_requested = True
        w.close()
        w.deleteLater()
        app.processEvents()
        gc.collect()
        print("[p3] construct w2", flush=True)
        w2 = MainWindow()           # 模拟 relogin 重建
        w2.deleteLater()
        app.processEvents()
        gc.collect()
        print("[p3] w2 done", flush=True)
        check(True, "MainWindow 退出登录再登录重建不崩")
    except Exception as exc:        # noqa: BLE001
        check(False, "MainWindow 重建抛出异常", "%s: %s" % (type(exc).__name__, exc))

    print()
    if fails:
        print("FAILED: %d 处 -> %s" % (len(fails), "; ".join(fails)))
        return 1
    print("ALL OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
