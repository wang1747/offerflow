"""OfferFlow 桌面版入口（原生 PySide6 界面 + 云端同步）。

账号、数据、AI 全部走云端 FastAPI，与网页版同一套后端、同一份数据。
"""

import os
import sys
import traceback


def _log_dir() -> str:
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


_CRASH_LOG = os.path.join(_log_dir(), "crash.log")


def _install_excepthook() -> None:
    """仅在发生未捕获异常时写入 crash.log（不产生空文件）。"""

    def _excepthook(tp, value, tb):
        try:
            with open(_CRASH_LOG, "a", encoding="utf-8") as f:
                f.write("=== uncaught exception ===\n")
                f.write("".join(traceback.format_exception(tp, value, tb)))
                f.write("\n")
        except Exception:
            pass
        sys.__excepthook__(tp, value, tb)

    sys.excepthook = _excepthook


def _setup_logging() -> None:
    """把日志落到文件（`DATA_DIR/logs/app.log`，1MB × 3 滚动）。

    为什么必须有：本地检索/解析的失败是**优雅降级**（不抛异常、界面照常出内容），
    若只在 stderr 打 warning，打包版没有控制台 → 等于没有痕迹，
    用户报"参考内容怪怪的"时无从查起。
    """
    import logging
    from logging.handlers import RotatingFileHandler

    try:
        from offerflow_desktop.config import DATA_DIR

        log_dir = os.path.join(DATA_DIR, "logs")
        os.makedirs(log_dir, exist_ok=True)
        handler = RotatingFileHandler(
            os.path.join(log_dir, "app.log"),
            maxBytes=1_000_000, backupCount=3, encoding="utf-8",
        )
        handler.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)s %(name)s: %(message)s"))
        root = logging.getLogger()
        root.setLevel(logging.INFO)
        root.addHandler(handler)
    except Exception:  # noqa: BLE001
        pass  # 日志系统起不来也绝不能拦住启动


def _seed_local_index() -> None:
    """首启把随包的「种子索引」铺到用户数据目录（幂等，失败不阻断启动）。

    为什么放在登录之前：本地索引里只有共享语料，与登录无关；
    早铺一秒，用户登录后第一次生成就立刻有离线参考可用（否则要等一次联网重建）。
    """
    import logging

    log = logging.getLogger(__name__)
    try:
        from offerflow_desktop.local_ai import seed

        info = seed.ensure_seeded()
        if info.get("seeded"):
            log.info("已铺设种子索引：%s（%s）", info.get("target"), info.get("files"))
        else:
            log.info("未铺设种子索引：%s", info.get("reason"))
    except Exception as exc:  # noqa: BLE001
        log.warning("铺设种子索引失败：%s: %s", type(exc).__name__, exc)


def _run_native() -> int:
    """PySide6 原生界面（云端同步版：账号/数据/AI 走云端）。"""
    from PySide6.QtWidgets import QApplication, QDialog

    from offerflow_desktop import api_client as api
    from offerflow_desktop import database as db
    from offerflow_desktop.config import APP_NAME
    from offerflow_desktop.ui.icons import app_icon
    from offerflow_desktop.ui.login import LoginDialog
    from offerflow_desktop.ui.main_window import MainWindow
    from offerflow_desktop.ui.theme import current_qss

    db.init_db()          # 本地仅保留系统配置（主题/记住账号等）
    api.load_auth()       # 恢复上次登录的云端 token
    _seed_local_index()   # 首启铺设随包种子索引（幂等；让离线检索开箱可用）

    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setWindowIcon(app_icon(64))

    # —— 单实例：第二次启动时激活已有窗口，不新开进程 ——
    from PySide6.QtNetwork import QLocalServer, QLocalSocket
    _ipc_name = APP_NAME + "_SingleInstance"

    # 先探测是否已有实例在监听（connectToServer 比 removeServer+listen 更可靠，
    # removeServer 会误删正在运行的实例的监听，导致单实例失效）。
    probe = QLocalSocket()
    probe.connectToServer(_ipc_name)
    if probe.waitForConnected(500):
        # 已有实例在运行：通知它激活窗口后，本进程直接退出
        probe.write(b"activate")
        probe.flush()
        probe.waitForBytesWritten(500)
        probe.disconnectFromServer()
        return 0

    # 没有已有实例：清理可能残留的管道，然后成为主实例
    QLocalServer.removeServer(_ipc_name)
    ipc_server = QLocalServer()
    ipc_server.listen(_ipc_name)

    while True:
        theme = db.get_system_config().get("theme", "light")
        app.setStyleSheet(current_qss(theme))
        # 同步进程内主题状态（手绘图标色 / 状态色板不跟随 QSS，需显式告知）
        from offerflow_desktop.ui import tokens
        tokens.set_theme(theme)

        login = LoginDialog()
        if login.exec() != QDialog.DialogCode.Accepted or not login.accepted_user:
            return 0
        # 云端 token 已由 api.login 写入 api_client，无需本地 set_active_user

        # 启动后台双向同步（硬编码 30 分钟一轮，不进设置、用户不可改）：
        # 上行本地待同步的改动、下行把服务器最新拉回本地。start() 幂等，
        # 重复登录不会起第二个线程；线程是 daemon，随进程退出。
        from offerflow_desktop.services import sync as sync_service
        sync_service.service().start()

        window = MainWindow()
        window.setup_tray()
        # 绑定单实例激活信号：后续再次双击图标时，会切到本窗口
        window.bind_single_instance(ipc_server)
        if db.get_system_config().get("theme") == "dark":
            window.reapply_icons(dark=True)

        # 双击图标即显示主窗口（已移除「启动时最小化到托盘」开关）
        window.show()

        app.exec()

        if not getattr(window, "logout_requested", False):
            return 0

    return 0


def _record_install_path() -> None:
    """把安装目录登记到 %LOCALAPPDATA%\\OfferFlow\\install_path.txt。

    安装程序（Inno Setup）升级时要先知道「旧版本装在哪」。它默认只查注册表
    卸载项，而该项在部分环境会丢失，导致升级被误判成全新安装（重弹目录页、
    并在目录已存在时弹确认框）。这里主动留一份自述文件作为兜底线索。

    仅对「安装程序装的版本」登记：目录里带 Inno 卸载器 unins000.exe 才算，
    避免便携解压版把用户的自定义目录登记成安装目录。
    """
    if not getattr(sys, "frozen", False):
        return
    try:
        install_dir = os.path.dirname(sys.executable)
        if not os.path.exists(os.path.join(install_dir, "unins000.exe")):
            return
        folder = os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"), "OfferFlow")
        os.makedirs(folder, exist_ok=True)
        # 必须带 BOM（utf-8-sig）：安装程序用 Inno 的 LoadStringsFromFile 读这个文件，
        # 而它只认「带 BOM 的 UTF-8」或 UTF-16LE，无 BOM 会按 ANSI 解码 —— 安装路径里
        # 有中文时就会读成乱码，导致升级探测这一路失效。
        with open(os.path.join(folder, "install_path.txt"), "w", encoding="utf-8-sig") as f:
            f.write(install_dir)
    except Exception:  # noqa: BLE001
        pass


def main() -> int:
    _install_excepthook()
    _setup_logging()
    _record_install_path()
    return _run_native()


if __name__ == "__main__":
    sys.exit(main())
