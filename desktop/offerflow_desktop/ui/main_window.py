"""工作台主窗口：深色渐变侧栏（图标导航）+ 页面堆栈 + 状态栏。"""

import os
import threading
import webbrowser
import logging
from typing import Optional

from PySide6.QtCore import Qt, QSize, QTimer
from PySide6.QtWidgets import (
    QApplication, QFrame, QHBoxLayout, QLabel, QMainWindow, QMenu, QMessageBox,
    QStackedWidget, QStatusBar, QSystemTrayIcon, QVBoxLayout, QWidget,
)

from .. import api_client as api
from .. import database as db
from .. import local_cache, local_store
from ..config import APP_NAME, APP_VERSION
from .icons import app_icon
from . import nav_registry as navreg
from . import tokens
from .nav_list import NavBrand, NavList, NavUserCard


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(APP_NAME)
        # 最小尺寸沿用原值（内容页在更窄/更矮时会挤）；导航溢出由滚动承接，
        # 所以这里**不需要**为了「能看全侧栏」而抬高最小值。
        self.setMinimumSize(QSize(1080, 700))
        self._apply_default_size()

        central = QWidget()
        self.setCentralWidget(central)
        root = QHBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # ===== 左侧导航 =====
        sidebar = QWidget()
        sidebar.setObjectName("SidebarPanel")
        sidebar.setFixedWidth(tokens.NAV_W)
        side_layout = QVBoxLayout(sidebar)
        side_layout.setContentsMargins(0, 0, 0, 0)
        side_layout.setSpacing(0)

        self.brand = NavBrand(APP_NAME, "求职全流程智能工作台")
        side_layout.addWidget(self.brand)

        self.nav = NavList()
        self.nav.navigated.connect(self._on_nav_activated)
        side_layout.addWidget(self.nav, 1)

        # ===== 状态栏（先于页面创建，页面构造时会调用 refresh_status） =====
        self.status = QStatusBar()
        self.setStatusBar(self.status)
        # 用户身份已经移到侧栏底部用户卡，状态栏只保留「AI 服务 + 数据同步」，
        # 避免同一条信息在一屏里出现两次。
        self.model_status_label = QLabel()
        self.data_status_label = QLabel()
        self.status.addWidget(self.model_status_label, 1)
        self.status.addPermanentWidget(self.data_status_label)
        self.status.setStyleSheet("QStatusBar::item { border: none; }")
        # AI 来源要读云端配置（走网络），先给占位值，随后异步刷新。
        # 必须在页面构造之前赋值：页面 __init__ 里会回调 refresh_status()。
        self._ai_source = "检测中…"

        self.stack = QStackedWidget()
        self.pages = {}
        self._nav_items = {}
        self._active_key: Optional[str] = None

        # 侧栏底部用户卡 + 版本（先建，页面构造期就会调 refresh_status）
        self.user_card = NavUserCard()
        self.user_card.logout_requested.connect(self._confirm_logout)
        self.user_card.profile_requested.connect(
            lambda: self.navigate("系统设置"))
        self.version_label = QLabel(f"桌面版 v{APP_VERSION}")
        self.version_label.setObjectName("SidebarVersion")

        # 按角色铺导航。角色先用本机缓存判断（不联网、不阻塞启动）；
        # 缓存里没有角色时（老版本登录态）留给 showEvent 的后台线程补查后追加。
        # 管理后台是**注册表里的一条普通条目**（admin_only=True），
        # 不再靠运行期往列表尾部塞 item —— 那种做法在窗口偏小时尾部够不到。
        self._build_nav()

        # ===== 右侧内容 =====
        content_wrap = QWidget()
        content_layout = QVBoxLayout(content_wrap)
        content_layout.setContentsMargins(0, 0, 0, 0)
        content_layout.addWidget(self._build_offline_banner())
        content_layout.addWidget(self.stack)

        side_layout.addWidget(self._build_nav_separator())
        side_layout.addWidget(self.user_card)
        side_layout.addWidget(self.version_label)

        root.addWidget(sidebar)
        root.addWidget(content_wrap, 1)

        # 落地页：显式 navigate 到注册表标记的首页。
        # 原先靠 setCurrentRow(0)，而 row 0 是**不可选的分组标题** ——
        # 选中态被丢掉，界面起来了侧栏却一条都不高亮（实测 16/16 False）。
        home = navreg.home_item()
        if home is not None:
            self.navigate(home.key)

        self.refresh_status()

        # 离线时定时探测服务器是否恢复（走后台线程，不在 UI 线程里等网络）
        self._probe_worker = None
        self._offline_timer = QTimer(self)
        self._offline_timer.setInterval(20000)
        self._offline_timer.timeout.connect(self._probe_online)
        self._offline_timer.start()

        self._watch_system_theme()

    # ===== 主题 =====

    def apply_theme(self, theme: str) -> None:
        """应用一个主题（允许传 ``"system"``，内部会解析成明/暗）。

        启动路径与设置页原先各写一遍「setStyleSheet + tokens.set_theme +
        重绘图标」，两处迟早会漂移 —— 比如只改了一处，手绘图标就跟不上。
        收敛到一个方法，谁要换主题都调它。
        """
        from PySide6.QtWidgets import QApplication
        from .theme import current_qss, resolve_theme

        app = QApplication.instance()
        if app is not None:
            app.setStyleSheet(current_qss(theme))
        # 内联样式与 QPainter 手绘色不跟随 QSS 重刷，必须显式告知当前档位
        resolved = resolve_theme(theme)
        tokens.set_theme(resolved)
        self.reapply_icons(dark=(resolved == "dark"))

    def _watch_system_theme(self) -> None:
        """「跟随系统」时把系统的明暗切换实时接过来。

        为什么做在窗口里而不是入口脚本：入口脚本的成员默认不在任何用例的
        覆盖范围里（"构造 ≠ 初始化"那条教训），而构造函数里挂的信号，
        构造型用例一定看得见。
        """
        try:
            from PySide6.QtWidgets import QApplication
            app = QApplication.instance()
            if app is None:
                return
            app.styleHints().colorSchemeChanged.connect(
                self._on_system_scheme_changed)
        except Exception:                                 # noqa: BLE001
            # 老 Qt 没有 colorSchemeChanged：跟随系统退化成"启动时读一次"，
            # 不该因此让窗口起不来。
            pass

    def _on_system_scheme_changed(self, *_args) -> None:
        """系统主题变了：只有当前设置确实是「跟随系统」才动界面。"""
        try:
            if str(db.get_system_config().get("theme") or "").strip() != "system":
                return
            self.apply_theme("system")
        except Exception:                                 # noqa: BLE001
            pass

    def _apply_default_size(self) -> None:
        """默认窗口尺寸：目标 1360×920，但**不得超过屏幕可用区域**。

        为什么不能写死：导航条目改成「主标题 + 副标题」两行后需要的高度变多，
        目标高度提到 920；而 1366×768 的笔记本可用区域往往只有 ~690，
        写死会把窗口撑到屏幕外面 —— 底部用户卡与版本号直接够不到
        （正是这次要修的「够不到」问题的另一种形态）。
        这里按屏幕可用区域收敛，并保证不低于 minimumSize。
        """
        want_w, want_h = 1360, 920
        screen = QApplication.primaryScreen()
        if screen is not None:
            avail = screen.availableGeometry()
            want_w = min(want_w, avail.width() - 60)
            want_h = min(want_h, avail.height() - 60)
        minimum = self.minimumSize()
        self.resize(max(minimum.width(), want_w),
                    max(minimum.height(), want_h))

    def _build_nav_separator(self) -> QFrame:
        """用户卡上方的细分隔线（把「导航」与「账号」两个语义块分开）。"""
        line = QFrame()
        line.setObjectName("NavSeparator")
        line.setFixedHeight(1)
        return line

    def _build_offline_banner(self) -> QFrame:
        """离线提示条（默认隐藏）。

        为什么必须有它：读接口原先在断网时返回空列表，界面显示「暂无数据」，
        用户会以为简历/投递记录全丢了。现在数据来自本地副本，
        但**必须明确告知这是副本、以及数据截至什么时候**——
        否则用户分不清「副本」和「刚刚更新的真实数据」。
        """
        self.offline_banner = QFrame()
        self.offline_banner.setObjectName("OfflineBanner")
        lay = QHBoxLayout(self.offline_banner)
        lay.setContentsMargins(16, 8, 16, 8)
        lay.setSpacing(8)
        self.offline_label = QLabel()
        self.offline_label.setObjectName("OfflineBannerText")
        self.offline_label.setWordWrap(True)
        lay.addWidget(self.offline_label, 1)
        self.offline_banner.hide()
        return self.offline_banner

    def _update_offline_banner(self) -> None:
        """按离线状态 / 待同步条数显示提示条（banner 未创建时安全跳过）。

        P4 后离线也能写：改动先落本地、联网后由后台同步自动补传。
        因此文案不再是「新增修改需联网才能生效」，而是「已存本地、待同步 N 条」。
        并且**有未同步改动时，即使当前在线也保留提示条**——否则同步一直失败
        时用户完全看不到（改动静默留本地，多端长期不一致）。
        """
        banner = getattr(self, "offline_banner", None)
        if banner is None:
            return
        offline = api.is_offline()
        pending = local_store.pending_count()
        if not offline and pending == 0:
            banner.hide()
            return
        if offline:
            reason = api.offline_reason() or "无法连接服务器"
            at = local_cache.synced_at()
            tail = (f"本地还有 {pending} 条改动待同步。" if pending
                    else "新增 / 修改会先存本地，联网后自动同步。")
            if at:
                when = at[:16].replace("T", " ")
                text = f"⚠ 离线模式：{reason}。显示本地数据（云端同步于 {when}）；{tail}"
            else:
                text = f"⚠ 离线模式：{reason}。本地暂无云端副本；{tail}"
        else:
            text = f"⟳ {pending} 条改动待同步至云端，后台会自动重试。"
        self.offline_label.setText(text)
        banner.show()

    def _probe_online(self) -> None:
        """离线期间每 20 秒探一次服务器是否恢复。

        只在离线时探；恢复后自动刷新状态栏与当前页数据，
        用户不必手动重启或切页。
        """
        if not api.is_offline() or self._probe_worker is not None:
            return
        from ..llm import LLMWorker

        worker = LLMWorker(api.ping)
        worker.success.connect(self._on_probe_result)
        worker.failed.connect(lambda _msg: self._on_probe_result(False))
        worker.start()
        self._probe_worker = worker

    def _on_probe_result(self, ok) -> None:
        self._probe_worker = None
        if not ok:
            return
        self.refresh_status()
        page = self.stack.currentWidget()
        refresh = getattr(page, "_refresh", None)
        if callable(refresh):
            try:
                refresh()
            except Exception:  # noqa: BLE001
                pass

    def _on_nav_activated(self, key: str) -> None:
        """侧栏条目被点击（NavList 已负责高亮，这里只切页面）。"""
        self.navigate(key)

    def _build_nav(self) -> None:
        """按当前角色铺侧栏 + 建页面（幂等，可重复调用）。"""
        is_admin = api.is_admin_cached()
        groups = navreg.groups_for(is_admin=is_admin)
        for group in groups:
            for item in group.items:
                self._register_page(item)
        self.nav.build(groups)

    def _register_page(self, item: "navreg.NavItem") -> None:
        """登记一页：建 widget、进页面栈、进 pages 字典（幂等）。"""
        if item.key in self.pages:
            return
        page = item.page(self)
        self.stack.addWidget(page)
        self.pages[item.key] = page
        self._nav_items[item.key] = item

    def _maybe_add_admin_page(self) -> None:
        """角色补查回来是管理员时，把「管理」分组补进侧栏（幂等）。

        这是**新增**而不是重建：用户可能已经点到某一页了，
        整体重建会把他弹回首页，属于「后台任务悄悄改了前台状态」。
        """
        if api.is_admin_cached() is not True:
            return
        if "管理后台" in self.pages:
            return
        admin_groups = [g for g in navreg.NAV_GROUPS if g.key == "admin"]
        if not admin_groups:
            return
        group = admin_groups[0]
        for item in group.items:
            self._register_page(item)
        self.nav.add_group(group, first=False)
        for item in group.items:
            self.nav.add_item(item)

    def open_agent(self, entry: Optional[dict]) -> None:
        """从业务页面带着上下文进入求职 Agent（对齐网页版 openCareerAgent.ts）。

        业务页面**只**通过这个方法进入 Agent——它们不直接调 ``api.agent.*``，
        只把上下文 id（jd_id / resume_id / application_id / session_id）交给这里，
        由 AgentPage 自己完成建/恢复会话。桌面端总按上下文创建/恢复，
        不来回传 session_id（刷新/历史返回语义才用 session_id）。
        """
        key = "求职 Agent"
        if not self.navigate(key):
            return
        page = self.pages[key]
        try:
            page.open_with(entry or {})
        except Exception as exc:  # noqa: BLE001
            # 审计 P1-2：原先把异常整段 pass 掉，业务页点了没反应也查不到原因。
            # 现在记完整 traceback，并在状态栏给用户一个可见提示。
            logging.getLogger(__name__).exception("打开求职 Agent 失败")
            self.status.showMessage("进入求职 Agent 失败：%s" % exc, 6000)

    def _confirm_logout(self) -> None:
        """侧栏「退出」：确认后回登录界面（与系统设置页的退出同一语义）。"""
        ret = QMessageBox.question(
            self, "退出登录", "确定要退出当前账号并返回登录界面吗？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if ret == QMessageBox.StandardButton.Yes:
            self.logout()

    def refresh_status(self) -> None:
        """刷新「侧栏用户卡 + 状态栏」。

        AI 来源需要请求云端，这里只消费缓存值，避免阻塞 UI；
        真正的拉取在 refresh_ai_source() 后台线程里做。
        """
        user = api.get_display_name() or api.get_active_user() or "未登录"
        role = {"admin": "管理员", "user": "成员"}.get(
            (api.get_active_role() or "").lower(), "")
        self.user_card.set_user(user, role)
        self.model_status_label.setText(f"AI 服务：{self._ai_source}")
        if api.is_offline():
            at = local_cache.synced_at()
            when = at[11:16] if at else "—"   # 只取 HH:MM
            self.data_status_label.setText(f"离线 · 本地副本（{when}）")
        else:
            self.data_status_label.setText("数据已同步云端")
        self._update_offline_banner()

    def refresh_ai_source(self) -> None:
        """后台拉取当前 AI 来源（平台默认模型 / 自有 Key），回来更新状态栏。

        注意：**不能**在工作线程里用 `QTimer.singleShot` —— 那个定时器归属于
        创建它的线程，工作线程没有事件循环，回调永远不会触发（已实测确认）。
        跨线程回到主线程必须靠 Qt 信号（信号会自动排队到接收者所在线程）。
        """
        from ..llm import LLMWorker

        worker = LLMWorker(api.describe_ai_source)
        worker.success.connect(self._apply_ai_source)
        worker.failed.connect(lambda _msg: self._apply_ai_source("获取失败"))
        worker.start()
        self._ai_source_worker = worker  # 持有引用，防止线程对象被 GC

    def _apply_ai_source(self, source: str) -> None:
        self._ai_source = source
        self.model_status_label.setText(f"AI 服务：{source}")

    def showEvent(self, event):
        super().showEvent(event)
        self.refresh_status()
        self.refresh_ai_source()
        self._load_profile_async()
        self._maybe_check_update()

    def _load_profile_async(self) -> None:
        """后台拉一次账号资料（只跑一次）。

        用途：① 同步昵称到状态栏；② 角色决定是否出现「管理后台」。
        登录响应不一定带 role，所以菜单判定不能只靠本机缓存 —— 但也不能在
        主线程联网，走后台线程 + 信号回主线程。
        """
        if getattr(self, "_profile_loaded", False):
            return
        self._profile_loaded = True

        from ..llm import LLMWorker

        worker = LLMWorker(api.get_profile)
        worker.success.connect(self._apply_profile)
        worker.failed.connect(lambda _msg: None)
        worker.start()
        self._profile_worker = worker  # 持有引用，防止线程对象被 GC

    def _apply_profile(self, profile) -> None:
        if not isinstance(profile, dict):
            return
        if str(profile.get("role") or "").lower() == "admin":
            self._maybe_add_admin_page()
        self.refresh_status()  # 昵称可能刚同步回来

    def _maybe_check_update(self) -> None:
        """启动后后台检查新版本，有更新则弹窗提示（只提醒、不自动装）。

        同上：结果必须经信号回主线程，否则弹窗不会出现。
        """
        if getattr(self, "_update_checked", False):
            return
        self._update_checked = True

        from ..llm import LLMWorker
        from ..services import updater

        worker = LLMWorker(updater.check_for_update)
        worker.success.connect(self._on_update_checked)
        worker.failed.connect(lambda _msg: None)
        worker.start()
        self._update_worker = worker

    def _on_update_checked(self, info) -> None:
        if info:
            self._show_update_dialog(info)

    def _show_update_dialog(self, info) -> None:
        hint = "点击「立即更新」会自动下载并覆盖安装到当前目录，装完自动重启；账号与数据不受影响。"
        box = QMessageBox(self)
        box.setWindowTitle("发现新版本")
        box.setText(f"检测到 OfferFlow 新版本 v{info['latest']}（当前 v{info['current']}）")
        box.setInformativeText((info.get("notes") + "\n\n" if info.get("notes") else "") + hint)
        update_btn = box.addButton("立即更新", QMessageBox.ButtonRole.AcceptRole)
        manual_btn = box.addButton("手动下载", QMessageBox.ButtonRole.ActionRole)
        box.addButton("稍后再说", QMessageBox.ButtonRole.RejectRole)
        box.exec()
        clicked = box.clickedButton()
        if clicked is update_btn:
            self._start_oneclick_update(info)
        elif clicked is manual_btn:
            try:
                webbrowser.open(info["download_url"])
            except Exception:  # noqa: BLE001
                pass

    def _start_oneclick_update(self, info) -> None:
        """一键更新：优先走增量（只下载真正变化的文件），不可用时回退全量安装包。

        增量需要「云端 manifest 可用 + 打包运行」两个条件；任一不满足就走原来的
        Inno Setup 全量安装器，保证任何情况下都不会卡住更新。
        """
        from ..services import updater

        if updater.incremental_supported():
            manifest = updater.fetch_manifest()
            if manifest and updater.is_newer(str(manifest.get("version") or ""), info["current"]):
                self._start_incremental_update(manifest, info)
                return
        self._start_full_update(info)

    def _start_incremental_update(self, manifest, info) -> None:
        """增量更新：比对本地哈希，只下载变化文件，再由引导脚本替换并重启。"""
        import tempfile

        from PySide6.QtWidgets import QProgressDialog

        from ..services import updater

        root = updater.install_dir()
        try:
            plan = updater.plan_incremental(manifest, root)
        except Exception as exc:  # noqa: BLE001
            self._start_full_update(info)
            return

        n_files = plan["total_files"]
        total_mb = plan["total_bytes"] / 1048576.0
        if n_files == 0 and not plan["remove"]:
            QMessageBox.information(
                self, "已经是最新",
                f"本地文件已与 v{manifest['version']} 完全一致，无需下载。",
            )
            return

        staging = tempfile.mkdtemp(prefix="OfferFlow-Upd-")
        state = {"done": 0, "total": plan["total_bytes"], "idx": 0, "count": n_files,
                 "ok": False, "err": "", "finished": False}

        dlg = QProgressDialog(
            f"正在下载 {n_files} 个更新文件（共 {total_mb:.1f} MB）…", "取消", 0, 100, self)
        dlg.setWindowTitle("更新 OfferFlow")
        dlg.setWindowModality(Qt.WindowModality.WindowModal)
        dlg.setMinimumDuration(0)
        dlg.setAutoClose(False)
        dlg.setAutoReset(False)
        dlg.setValue(0)
        dlg.show()
        QApplication.processEvents()

        def _progress(done: int, total: int, idx: int, count: int) -> None:
            state["done"], state["total"], state["idx"], state["count"] = done, total, idx, count

        def _worker() -> None:
            try:
                res = updater.stage_updates(manifest, root, staging, progress=_progress)
                state["ok"] = bool(res.get("ok"))
                state["err"] = str(res.get("error") or "")
            except Exception as exc:  # noqa: BLE001
                state["err"] = str(exc)
            finally:
                state["finished"] = True

        threading.Thread(target=_worker, daemon=True).start()

        timer = QTimer(self)
        timer.setInterval(200)

        def _tick() -> None:
            if dlg.wasCanceled():
                state["cancelled"] = True
                timer.stop()
                dlg.close()
                return
            total = state["total"]
            if total:
                dlg.setLabelText(
                    f"增量更新 {state['idx']}/{state['count']} 个文件　"
                    f"{state['done'] / 1048576:.1f} / {total / 1048576:.1f} MB"
                )
                dlg.setValue(min(100, int(state["done"] * 100 / total)))
            if state["finished"]:
                timer.stop()
                dlg.close()
                if state["ok"]:
                    self._apply_incremental(staging, manifest, info)
                else:
                    # 增量失败绝不硬撑：回退到全量安装包，用户始终能升上去。
                    box = QMessageBox(self)
                    box.setWindowTitle("增量更新失败")
                    box.setText(f"增量更新未能完成：{state['err'] or '未知原因'}")
                    box.setInformativeText("已自动改用完整安装包重试（约 119MB）。")
                    retry = box.addButton("改用完整包更新", QMessageBox.ButtonRole.AcceptRole)
                    box.addButton("手动下载", QMessageBox.ButtonRole.ActionRole)
                    box.addButton("取消", QMessageBox.ButtonRole.RejectRole)
                    box.exec()
                    if box.clickedButton() is retry:
                        self._start_full_update(info)
                    elif box.clickedButton() is not None and \
                            box.clickedButton().text() == "手动下载":
                        try:
                            webbrowser.open(info["download_url"])
                        except Exception:  # noqa: BLE001
                            pass

        timer.timeout.connect(_tick)
        timer.start()

    def _apply_incremental(self, staging: str, manifest, info) -> None:
        """确认后交给引导脚本替换文件并重启（必须先退出本进程，否则 exe 占用）。"""
        from ..services import updater

        ans = QMessageBox.question(
            self,
            "准备更新",
            f"v{manifest['version']} 的更新文件已下载完成。\n\n"
            "点击「立即安装」后 OfferFlow 会自动关闭并在数秒内完成替换、重新打开；"
            "账号与数据不受影响。",
            QMessageBox.StandardButton.Ok | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Ok,
        )
        if ans != QMessageBox.StandardButton.Ok:
            return
        script = updater.write_apply_script(updater.install_dir(), staging, manifest, os.getpid())
        if not updater.launch_apply_and_exit(script):
            QMessageBox.warning(self, "更新失败", "无法启动更新脚本，请改用「手动下载」。")
            return
        try:
            if getattr(self, "tray", None):
                self.tray.hide()
        except Exception:  # noqa: BLE001
            pass
        QApplication.quit()
        os._exit(0)

    def _start_full_update(self, info) -> None:
        """一键更新（全量）：后台下载完整安装包 → 进度提示 → 静默覆盖安装。"""
        from PySide6.QtWidgets import QProgressDialog

        from ..services import updater
        from ... import database as db

        dest = updater.setup_cache_path(info["latest"], db.resolve_download_dir())
        state = {"pct": 0, "done": 0, "total": 0, "ok": False, "err": "", "finished": False}

        dlg = QProgressDialog("正在下载新版本…", "取消", 0, 100, self)
        dlg.setWindowTitle("更新 OfferFlow")
        dlg.setWindowModality(Qt.WindowModality.WindowModal)
        dlg.setMinimumDuration(0)
        dlg.setAutoClose(False)
        dlg.setAutoReset(False)
        dlg.setValue(0)
        dlg.show()
        QApplication.processEvents()

        def _progress(done: int, total: int) -> None:
            state["done"] = done
            state["total"] = total
            if total:
                state["pct"] = int(done * 100 / total)

        def _worker() -> None:
            try:
                updater.download_installer(
                    info["download_url"], dest, progress=_progress,
                    expected_sha256=info.get("sha256") or None,
                )
                state["ok"] = True
            except Exception as exc:  # noqa: BLE001
                state["err"] = str(exc)
            finally:
                state["finished"] = True

        threading.Thread(target=_worker, daemon=True).start()

        timer = QTimer(self)
        timer.setInterval(200)

        def _tick() -> None:
            if dlg.wasCanceled():
                state["cancelled"] = True
                timer.stop()
                dlg.close()
                return
            total = state["total"]
            if total:
                dlg.setLabelText(
                    f"正在下载新版本… {state['done'] / 1048576:.1f} / {total / 1048576:.1f} MB"
                )
                dlg.setValue(state["pct"])
            if state["finished"]:
                timer.stop()
                dlg.close()
                if state["ok"]:
                    self._apply_update(dest, info)
                elif state["err"]:
                    QMessageBox.warning(
                        self, "更新失败",
                        f"下载安装包失败：{state['err']}\n\n可改用「手动下载」。",
                    )

        timer.timeout.connect(_tick)
        timer.start()

    def _apply_update(self, setup_path: str, info) -> None:
        """确认后静默覆盖安装，并退出本进程把舞台交给安装程序。"""
        from ..services import updater

        ans = QMessageBox.question(
            self,
            "准备更新",
            f"新版 v{info['latest']} 已下载完成。\n\n"
            "点击「立即安装」后 OfferFlow 会自动关闭，安装程序在后台完成覆盖升级"
            "（沿用当前安装目录，不需要再选目录），装完自动重新打开。",
            QMessageBox.StandardButton.Ok | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Ok,
        )
        if ans != QMessageBox.StandardButton.Ok:
            return
        if not updater.run_silent_upgrade(setup_path):
            QMessageBox.warning(self, "更新失败", "无法启动安装程序，请改用「手动下载」。")
            return
        try:
            if getattr(self, "tray", None):
                self.tray.hide()
        except Exception:  # noqa: BLE001
            pass
        QApplication.quit()
        os._exit(0)

    def navigate(self, page_key: str) -> bool:
        """按页面键名跳转，并同步侧栏高亮。返回是否命中（未注册的键返回 False）。

        页面切换与高亮**必须在一起**：原先切页与高亮是两条独立路径
        （``_on_nav_changed`` 改高亮、``navigate`` 手动遍历列表改高亮），
        于是「用代码切了页但高亮没跟上」和「高亮改了但页面没切」都有可能发生。
        现在统一从这里走，两侧不可能再不一致。
        """
        page = self.pages.get(page_key)
        if page is None:
            return False
        self.stack.setCurrentWidget(page)
        self._active_key = page_key
        self.nav.set_active(page_key)
        return True

    def reapply_icons(self, dark: bool = False) -> None:
        """主题切换后重绘侧栏图标（深色主题用更亮的灰/靛）。

        QSS 能自动跟随主题重刷，但导航图标是 QPixmap（QPainter 手绘），
        不会自己变 —— 必须显式重画，否则深色主题下图标仍是深灰、几乎看不见。
        """
        self.nav.set_dark(dark)

    # ===== 系统托盘（后台运行） =====
    def setup_tray(self) -> None:
        self._force_quit = False
        self.logout_requested = False
        self.tray = QSystemTrayIcon(app_icon(32))
        self.tray.setToolTip(APP_NAME)
        menu = QMenu()
        show_action = menu.addAction("显示主界面")
        show_action.triggered.connect(self.show_and_activate)
        quit_action = menu.addAction("退出")
        quit_action.triggered.connect(self.quit_app)
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(self._on_tray_activated)
        self.tray.show()

    def _on_tray_activated(self, reason) -> None:
        if reason == QSystemTrayIcon.ActivationReason.DoubleClick:
            self.show_and_activate()

    def show_and_activate(self) -> None:
        self.show()
        if self.isMinimized():
            self.showNormal()
        self.raise_()
        self.activateWindow()

    def bind_single_instance(self, server) -> None:
        """单实例 IPC：第二次启动时经此激活本窗口。"""
        self._ipc_server = server
        server.newConnection.connect(self._on_activate_request)

    def _on_activate_request(self) -> None:
        sock = self._ipc_server.nextPendingConnection()
        if sock is not None:
            sock.readyRead.connect(self._activate_from_ipc)
            sock.disconnected.connect(sock.deleteLater)

    def _activate_from_ipc(self) -> None:
        self.show_and_activate()

    def quit_app(self) -> None:
        self._force_quit = True
        self.tray.hide()
        QApplication.instance().quit()

    def logout(self) -> None:
        """退出登录：关闭窗口与托盘，回到登录界面。"""
        self._force_quit = True
        self.logout_requested = True
        self.tray.hide()
        self.close()
        QApplication.instance().quit()

    def closeEvent(self, event):
        if getattr(self, "tray", None) and self.tray.isVisible() and not self._force_quit:
            event.ignore()
            self.hide()
            self.tray.showMessage(
                APP_NAME, "已最小化到系统托盘，后台继续运行",
                QSystemTrayIcon.MessageIcon.Information, 2000,
            )
        else:
            event.accept()
