"""桌面版外壳：用 WebView 直接加载网页版。

设计取舍（为什么是壳，而不是把网页功能在 PySide6 里重写一遍）：
  - 网页版包含管理后台、配额、审计等平台功能，逐项重写工作量大且两套代码必然漂移；
  - 用 WebView 加载后，**桌面版与网页版功能天然 100% 一致**，后续只需维护网页版一处。

因此这里只负责「原生外壳」该管的事：
  登录态持久化（cookie/localStorage 落盘）、文件下载、外链交给系统浏览器、
  断网时的友好重试页、窗口尺寸记忆、自动检查更新。
"""

from __future__ import annotations

import json
import os
import threading

from PySide6.QtCore import Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QAction, QDesktopServices, QKeySequence
from PySide6.QtWebEngineCore import (
    QWebEnginePage,
    QWebEngineProfile,
    QWebEngineSettings,
)
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWidgets import (
    QApplication,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMenu,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from ..config import APP_NAME, APP_VERSION, DATA_DIR

# 外壳自身的配置（窗口尺寸等），与业务无关，单独存一个文件
SHELL_STATE = os.path.join(DATA_DIR, "shell_state.json")

# 允许在壳内导航的域名（站内）。其余一律交给系统浏览器，
# 避免用户点外链时被关在一个没有地址栏的窗口里出不来。
def _allowed_hosts() -> set[str]:
    try:
        from ..services import cloud

        host = QUrl(cloud.get_cloud_url()).host()
        return {host} if host else set()
    except Exception:  # noqa: BLE001
        return set()


class _ShellPage(QWebEnginePage):
    """把外链与 target=_blank 的跳转都收拢到壳内/系统浏览器。"""

    def __init__(self, profile: QWebEngineProfile, parent=None):
        super().__init__(profile, parent)
        self._allowed = _allowed_hosts()

    def acceptNavigationRequest(self, url: QUrl, nav_type, is_main_frame: bool) -> bool:  # noqa: N802
        # 只拦主框架的跨站跳转；子资源（图片/接口）照常放行
        if is_main_frame and url.scheme() in ("http", "https"):
            host = url.host()
            if host and host not in self._allowed:
                QDesktopServices.openUrl(url)
                return False
        return super().acceptNavigationRequest(url, nav_type, is_main_frame)

    def createWindow(self, _type):  # noqa: N802
        # 网页里 window.open / target=_blank：复用当前视图，不开新窗口
        return self


class ShellWebView(QWebEngineView):
    """右键菜单本地化为中文（QtWebEngine 默认菜单是英文，观感突兀）。"""

    def contextMenuEvent(self, event):  # noqa: N802
        page = self.page()
        menu = QMenu(self)

        back = menu.addAction("后退")
        back.setEnabled(self.history().canGoBack())
        back.triggered.connect(self.back)

        forward = menu.addAction("前进")
        forward.setEnabled(self.history().canGoForward())
        forward.triggered.connect(self.forward)

        menu.addSeparator()
        menu.addAction("刷新", self.reload)
        menu.addSeparator()

        # 复用页面自带的编辑动作，但把英文文案换掉
        for action, label in (
            (QWebEnginePage.WebAction.Cut, "剪切"),
            (QWebEnginePage.WebAction.Copy, "复制"),
            (QWebEnginePage.WebAction.Paste, "粘贴"),
            (QWebEnginePage.WebAction.SelectAll, "全选"),
        ):
            act = page.action(action)
            if act is not None:
                act.setText(label)
                menu.addAction(act)

        menu.exec(self._event_pos(event))

    @staticmethod
    def _event_pos(event):
        """Qt6 用 globalPosition()，老版本才用 globalPos()。"""
        if hasattr(event, "globalPosition"):
            return event.globalPosition().toPoint()
        return event.globalPos()


class WebShellWindow(QMainWindow):
    """主窗口：标题栏 + 菜单 + WebView + 状态栏。"""

    _update_ready = Signal(dict)

    def __init__(self, base_url: str):
        super().__init__()
        self._base = base_url.rstrip("/")
        self._zoom = 1.0
        self._manual_update_check = False

        self.setWindowTitle(APP_NAME)
        self.setMinimumSize(420, 560)
        self._restore_geometry()

        # ---- 持久化 profile：登录态（cookie + localStorage）跨次启动保留 ----
        storage = os.path.join(DATA_DIR, "webengine")
        os.makedirs(storage, exist_ok=True)
        self._profile = QWebEngineProfile("offerflow-shell", self)
        self._profile.setPersistentStoragePath(storage)
        self._profile.setCachePath(os.path.join(storage, "cache"))
        self._profile.setPersistentCookiesPolicy(
            QWebEngineProfile.PersistentCookiesPolicy.ForcePersistentCookies
        )
        self._profile.downloadRequested.connect(self._on_download)

        # ---- 页面与视图 ----
        self._page = _ShellPage(self._profile, self)
        self._view = ShellWebView(self)
        self._view.setPage(self._page)
        settings = self._view.settings()
        settings.setAttribute(QWebEngineSettings.WebAttribute.LocalStorageEnabled, True)
        settings.setAttribute(QWebEngineSettings.WebAttribute.JavascriptEnabled, True)
        settings.setAttribute(QWebEngineSettings.WebAttribute.ScrollAnimatorEnabled, True)
        settings.setAttribute(QWebEngineSettings.WebAttribute.FocusOnNavigationEnabled, True)

        # ---- 断网时的重试页（堆叠切换） ----
        self._stack = QStackedWidget(self)
        self._stack.addWidget(self._view)
        self._stack.addWidget(self._build_offline_page())
        self.setCentralWidget(self._stack)

        # ---- 状态栏 + 细进度条 ----
        self._progress = QProgressBar(self)
        self._progress.setMaximumWidth(180)
        self._progress.setTextVisible(False)
        self._progress.setFixedHeight(4)
        self._progress.hide()
        self.statusBar().addPermanentWidget(self._progress)
        self.statusBar().showMessage("正在连接服务器…")

        self._build_menus()

        self._view.loadStarted.connect(self._on_load_started)
        self._view.loadProgress.connect(self._progress.setValue)
        self._view.loadFinished.connect(self._on_load_finished)
        self._view.titleChanged.connect(self._on_title_changed)
        self._page.linkHovered.connect(
            lambda url: self.statusBar().showMessage(url, 4000)
        )
        self._update_ready.connect(self._on_update_ready)

        self._view.setUrl(QUrl(self._base + "/"))

        # 启动后延迟几秒静默检查更新（不阻塞首屏）
        QTimer.singleShot(3000, lambda: self._check_update(silent=True))

    # ------------------------------------------------------------------ UI

    def _build_offline_page(self) -> QWidget:
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.setSpacing(16)

        title = QLabel("无法连接到服务器")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        title.setStyleSheet("font-size: 20px; font-weight: 700; color: #0f172a;")

        self._offline_detail = QLabel(f"请确认网络正常，或稍后重试。\n服务器地址：{self._base}")
        self._offline_detail.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._offline_detail.setWordWrap(True)
        self._offline_detail.setStyleSheet("color: #64748b; font-size: 13px;")

        retry = QPushButton("重新连接")
        retry.setFixedWidth(140)
        retry.setCursor(Qt.CursorShape.PointingHandCursor)
        retry.setStyleSheet(
            "QPushButton { background:#4f46e5; color:white; border:none; border-radius:10px;"
            " padding:10px 18px; font-size:14px; font-weight:600; }"
            "QPushButton:hover { background:#4338ca; }"
        )
        retry.clicked.connect(lambda: self._view.setUrl(QUrl(self._base + "/")))

        row = QHBoxLayout()
        row.addStretch(1)
        row.addWidget(retry)
        row.addStretch(1)

        lay.addWidget(title)
        lay.addWidget(self._offline_detail)
        lay.addLayout(row)
        return page

    def _build_menus(self) -> None:
        bar = self.menuBar()

        m_file = bar.addMenu("文件(&F)")
        self._plug(m_file, "刷新", "F5", self._view.reload)
        self._plug(m_file, "回到首页", "Ctrl+H", lambda: self._view.setUrl(QUrl(self._base + "/")))
        m_file.addSeparator()
        self._plug(m_file, "在浏览器中打开", None, lambda: QDesktopServices.openUrl(QUrl(self._base + "/")))
        m_file.addSeparator()
        self._plug(m_file, "退出", "Ctrl+Q", self.close)

        m_view = bar.addMenu("视图(&V)")
        self._plug(m_view, "放大", "Ctrl+=", lambda: self._set_zoom(self._zoom + 0.1))
        self._plug(m_view, "缩小", "Ctrl+-", lambda: self._set_zoom(self._zoom - 0.1))
        self._plug(m_view, "重置缩放", "Ctrl+0", lambda: self._set_zoom(1.0))
        m_view.addSeparator()
        self._plug(m_view, "全屏", "F11", self._toggle_fullscreen)

        m_help = bar.addMenu("帮助(&H)")
        self._plug(m_help, "检查更新", None, lambda: self._check_update(silent=False))
        self._plug(m_help, "关于 OfferFlow", None, self._about)

    @staticmethod
    def _plug(menu: QMenu, text: str, shortcut: str | None, slot) -> QAction:
        act = QAction(text, menu)
        if shortcut:
            act.setShortcut(QKeySequence(shortcut))
        act.triggered.connect(slot)
        menu.addAction(act)
        return act

    # ------------------------------------------------------------- 行为

    def _set_zoom(self, value: float) -> None:
        self._zoom = max(0.6, min(2.0, round(value, 2)))
        self._view.setZoomFactor(self._zoom)
        self.statusBar().showMessage(f"缩放 {int(self._zoom * 100)}%", 2500)

    def _toggle_fullscreen(self) -> None:
        if self.isFullScreen():
            self.showNormal()
        else:
            self.showFullScreen()

    def _on_title_changed(self, title: str) -> None:
        title = (title or "").strip()
        self.setWindowTitle(f"{title} · {APP_NAME}" if title and title != "OfferFlow" else APP_NAME)

    def _on_load_started(self) -> None:
        self._progress.setValue(0)
        self._progress.show()
        self.statusBar().showMessage("加载中…")

    def _on_load_finished(self, ok: bool) -> None:
        self._progress.hide()
        if ok:
            self._stack.setCurrentWidget(self._view)
            self.statusBar().showMessage("就绪", 3000)
        else:
            self._offline_detail.setText(
                f"请确认网络正常，或稍后重试。\n服务器地址：{self._base}"
            )
            self._stack.setCurrentWidget(self._stack.widget(1))
            self.statusBar().showMessage("连接失败", 5000)

    def _on_download(self, item) -> None:
        """网页版里的导出（CSV / 简历文件等）走原生「另存为」。"""
        try:
            suggested = item.suggestedFileName() or "download"
            default_dir = os.path.join(os.path.expanduser("~"), "Downloads")
            target, _ = QFileDialog.getSaveFileName(
                self, "保存文件", os.path.join(default_dir, suggested)
            )
            if not target:
                item.cancel()
                return
            item.setDownloadDirectory(os.path.dirname(target))
            item.setDownloadFileName(os.path.basename(target))
            item.accept()
            self.statusBar().showMessage(f"已开始下载：{os.path.basename(target)}", 6000)
        except Exception:  # noqa: BLE001
            try:
                item.cancel()
            except Exception:  # noqa: BLE001
                pass

    # ---------------------------------------------------------- 更新检查

    def _check_update(self, silent: bool) -> None:
        self._manual_update_check = not silent
        if not silent:
            self.statusBar().showMessage("正在检查更新…", 3000)

        def work() -> None:
            try:
                from ..services import updater

                info = updater.check_for_update()
            except Exception:  # noqa: BLE001
                info = None
            self._update_ready.emit(info or {})

        threading.Thread(target=work, daemon=True).start()

    def _on_update_ready(self, info: dict) -> None:
        if not info:
            if self._manual_update_check:
                QMessageBox.information(
                    self, "检查更新", f"当前已是最新版本（v{APP_VERSION}）。"
                )
            return
        latest = info.get("latest", "")
        notes = info.get("notes") or "（无更新说明）"
        answer = QMessageBox.question(
            self,
            "发现新版本",
            f"检测到新版本 v{latest}（当前 v{info.get('current', APP_VERSION)}）。\n\n"
            f"{notes}\n\n是否前往下载页面？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.Yes,
        )
        if answer == QMessageBox.StandardButton.Yes:
            QDesktopServices.openUrl(QUrl(info.get("download_url", self._base)))

    def _about(self) -> None:
        QMessageBox.about(
            self,
            "关于 OfferFlow",
            f"<b>{APP_NAME}</b><br>版本 v{APP_VERSION}<br><br>"
            "桌面版以 WebView 加载同一套网页应用，<br>"
            "功能与网页版完全一致（含管理后台）。<br><br>"
            f"服务地址：{self._base}",
        )

    # ------------------------------------------------------- 窗口尺寸记忆

    def _restore_geometry(self) -> None:
        try:
            with open(SHELL_STATE, "r", encoding="utf-8") as f:
                state = json.load(f)
            w = int(state.get("width") or 0)
            h = int(state.get("height") or 0)
            if w > 0 and h > 0:
                self.resize(w, h)
            else:
                self.resize(1280, 860)
            if state.get("maximized"):
                self.showMaximized()
        except Exception:  # noqa: BLE001
            self.resize(1280, 860)

    def _save_geometry(self) -> None:
        try:
            maximized = self.isMaximized()
            size = self.normalGeometry().size() if maximized else self.size()
            with open(SHELL_STATE, "w", encoding="utf-8") as f:
                json.dump(
                    {"width": size.width(), "height": size.height(), "maximized": maximized},
                    f,
                    ensure_ascii=False,
                    indent=2,
                )
        except Exception:  # noqa: BLE001
            pass

    def closeEvent(self, event):  # noqa: N802
        self._save_geometry()
        super().closeEvent(event)


def run_shell() -> int:
    """启动桌面外壳。"""
    try:
        from ..services import cloud

        base = cloud.get_cloud_url()
    except Exception:  # noqa: BLE001
        base = "http://localhost:8001"

    # QtWebEngine 要求 QApplication 先于 QWebEngineView 创建
    app = QApplication.instance() or QApplication([])
    app.setApplicationName(APP_NAME)

    try:
        from .icons import app_icon

        app.setWindowIcon(app_icon(64))
    except Exception:  # noqa: BLE001
        pass

    window = WebShellWindow(base)
    window.show()
    return app.exec()
