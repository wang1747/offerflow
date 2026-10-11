"""系统设置页：外观（主题）+ AI 模型 + 文件（导出目录）+ 启动行为。"""

import json
import os
import sys
from datetime import date

from PySide6.QtCore import Qt
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QFileDialog, QFormLayout, QFrame,
    QHBoxLayout, QLabel, QLineEdit, QPushButton, QScrollArea, QVBoxLayout, QWidget,
)

from ... import api_client as api
from ... import database as db
from ..change_email import ChangeEmailDialog
from ..components import api_guard, card, hint_label, make_title, section_label


def _set_auto_start(enabled: bool) -> bool:
    """写/删注册表开机自启项（HKCU\\...\\Run）。"""
    try:
        import winreg
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Run",
            0, winreg.KEY_SET_VALUE,
        )
        if enabled:
            exe = sys.executable
            winreg.SetValueEx(key, "OfferFlow", 0, winreg.REG_SZ, f'"{exe}"')
        else:
            try:
                winreg.DeleteValue(key, "OfferFlow")
            except FileNotFoundError:
                pass
        winreg.CloseKey(key)
        return True
    except Exception:
        return False


class SystemSettingsPage(QWidget):
    def __init__(self, main_window):
        super().__init__()
        self.mw = main_window
        self._worker = None

        # 外层布局
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # 滚动区域（内容多，避免输入框被压缩）
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        content = QWidget()
        layout = QVBoxLayout(content)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.setSpacing(16)
        layout.addWidget(make_title(
            "系统设置",
            "外观、AI 模型、导出文件位置与启动行为。配置保存在本机，不随账号变化。",
        ))

        layout.addWidget(self._account_card())
        layout.addWidget(self._appearance_card())
        layout.addWidget(self._model_card())
        layout.addWidget(self._file_card())
        layout.addWidget(self._download_card())
        layout.addWidget(self._privacy_card())
        layout.addWidget(self._startup_card())
        layout.addStretch(1)

        scroll.setWidget(content)
        outer.addWidget(scroll)

        self._load()

    # ===== 账号 =====
    def _account_card(self) -> QFrame:
        frame = card()
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(12)

        # 上半：头像 + 昵称 + 邮箱
        head = QHBoxLayout()
        head.setSpacing(12)
        self.avatar_label = QLabel("👤")
        self.avatar_label.setFixedSize(48, 48)
        self.avatar_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.avatar_label.setStyleSheet("font-size: 22px; border-radius: 24px; background: #e5e7eb;")
        info = QVBoxLayout()
        info.setSpacing(4)
        self.account_name_label = QLabel("")
        self.account_name_label.setStyleSheet("font-size: 14px; font-weight: 700;")
        self.account_hint_label = QLabel("你的简历、岗位、投递等数据与网页版实时同步")
        self.account_hint_label.setProperty("class", "hint")
        info.addWidget(self.account_name_label)
        info.addWidget(self.account_hint_label)
        head.addWidget(self.avatar_label)
        head.addLayout(info, 1)
        layout.addLayout(head)

        # 下半：账号操作按钮（头像 / 昵称 / 密码 / 换绑邮箱 / 退出）
        row = QHBoxLayout()
        row.setSpacing(8)
        self.avatar_btn = QPushButton("更换头像")
        self.avatar_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.avatar_btn.clicked.connect(self._open_avatar)
        self.nickname_btn = QPushButton("修改昵称")
        self.nickname_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.nickname_btn.clicked.connect(self._open_nickname)
        self.password_btn = QPushButton("修改密码")
        self.password_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.password_btn.clicked.connect(self._open_change_password)
        change_btn = QPushButton("换绑邮箱")
        change_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        change_btn.clicked.connect(self._open_change_email)
        logout_btn = QPushButton("退出登录")
        logout_btn.setProperty("class", "danger")
        logout_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        logout_btn.clicked.connect(self._confirm_logout)
        row.addWidget(self.avatar_btn)
        row.addWidget(self.nickname_btn)
        row.addWidget(self.password_btn)
        row.addWidget(change_btn)
        row.addStretch(1)
        row.addWidget(logout_btn)
        layout.addLayout(row)
        return frame

    def _open_avatar(self) -> None:
        """更换头像：选图 -> 上传 -> 刷新显示 + 本地登录态。"""
        from PySide6.QtWidgets import QMessageBox

        path, _ = QFileDialog.getOpenFileName(
            self, "选择头像图片", "", "图片 (*.jpg *.jpeg *.png *.gif *.webp)")
        if not path:
            return
        try:
            user = api.upload_avatar(path)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, "上传失败", str(exc))
            return
        if isinstance(user, dict) and user.get("avatar"):
            api.set_user(user)
            self._render_avatar()
            self.mw.refresh_status()
            QMessageBox.information(self, "上传成功", "头像已更新")
        else:
            QMessageBox.warning(self, "上传失败", "服务器未返回头像地址")

    def _render_avatar(self) -> None:
        """把当前账号的头像显示到头像框（有 URL 就下载显示，否则占位符）。"""
        u = api.get_user() or {}
        avatar = u.get("avatar") or ""
        if not avatar:
            self.avatar_label.setText("👤")
            self.avatar_label.setStyleSheet(
                "font-size: 22px; border-radius: 24px; background: #e5e7eb;")
            return
        data = api.download_file(avatar)
        if data:
            pix = QPixmap()
            pix.loadFromData(data)
            if not pix.isNull():
                pix = pix.scaled(48, 48, Qt.AspectRatioMode.KeepAspectRatio,
                                 Qt.TransformationMode.SmoothTransformation)
                self.avatar_label.setPixmap(pix)
                self.avatar_label.setStyleSheet("border-radius: 24px;")
                return
        self.avatar_label.setText("👤")
        self.avatar_label.setStyleSheet(
            "font-size: 22px; border-radius: 24px; background: #e5e7eb;")

    def _open_nickname(self) -> None:
        """修改昵称（与网页版 PATCH /auth/profile 同一套接口）。"""
        from PySide6.QtWidgets import QMessageBox

        from ..profile_dialogs import NicknameDialog

        prof = api.get_profile()
        dlg = NicknameDialog(self, current_name=prof.get("name") or "")
        if dlg.exec() != QDialog.DialogCode.Accepted or not dlg.new_name:
            return
        self._render_account()
        self.mw.refresh_status()
        QMessageBox.information(self, "修改成功", f"昵称已改为：{dlg.new_name}")

    def _open_change_password(self) -> None:
        """修改登录密码（旧密码校验在服务端做）。"""
        from PySide6.QtWidgets import QMessageBox

        from ..profile_dialogs import ChangePasswordDialog

        dlg = ChangePasswordDialog(self)
        dlg.exec()
        if dlg.changed:
            QMessageBox.information(
                self, "密码已修改",
                "登录密码已更新。\n\n下次登录请使用新密码；"
                "如果你在网页版或其它设备也登录着，建议一并重新登录。",
            )

    def _open_change_email(self) -> None:
        """换绑登录邮箱（与网页版「设置 → 登录邮箱」同一套接口）。"""
        from PySide6.QtWidgets import QMessageBox

        old_email = api.get_active_user() or ""
        dlg = ChangeEmailDialog(self, current_email=old_email)
        if dlg.exec() != QDialog.DialogCode.Accepted or not dlg.new_email:
            return
        new_email = dlg.new_email

        # 邮箱是**唯一登录账号**：本机「记住账号」必须跟着改，
        # 否则下次登录仍然拿旧邮箱去登，直接失败。
        saved_user, saved_pwd = db.get_remembered_login()
        if saved_user:
            db.save_remembered_login(new_email, saved_pwd,
                                     remember_user=True, remember_pwd=bool(saved_pwd))

        self.account_name_label.setText(new_email)
        self.mw.refresh_status()
        QMessageBox.information(
            self, "换绑成功",
            f"登录邮箱已改为：\n{new_email}\n\n下次登录请使用新邮箱。",
        )

    def _confirm_logout(self) -> None:
        from PySide6.QtWidgets import QMessageBox
        ret = QMessageBox.question(
            self, "退出登录", "确定要退出当前账号并返回登录界面吗？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if ret == QMessageBox.StandardButton.Yes:
            self.mw.logout()

    # ===== 外观（主题） =====
    def _appearance_card(self) -> QFrame:
        frame = card()
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(12)
        layout.addWidget(section_label("外观"))
        form = QFormLayout()
        form.setSpacing(12)
        self.theme_combo = QComboBox()
        self.theme_combo.addItem("跟随系统", "system")
        self.theme_combo.addItem("浅色", "light")
        self.theme_combo.addItem("深色", "dark")
        self.theme_combo.currentIndexChanged.connect(self._on_theme_changed)
        form.addRow("主题", self.theme_combo)
        layout.addLayout(form)
        layout.addWidget(hint_label(
            "切换主题即时生效。「跟随系统」会跟着 Windows 的明暗设置走，"
            "系统切换时界面也会跟着变。"
        ))
        return frame

    # ===== AI 服务（云端 / 自有 Key） =====
    def _model_card(self) -> QFrame:
        frame = card()
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(12)
        layout.addWidget(section_label("AI 服务"))
        row = QHBoxLayout()
        row.setSpacing(12)
        self.ai_source_label = QLabel("AI 服务：—")
        self.ai_source_label.setStyleSheet("font-size: 13px; font-weight: 600;")
        self.ai_source_label.setWordWrap(True)
        open_btn = QPushButton("打开模型设置")
        open_btn.setProperty("class", "ghost")
        open_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        open_btn.clicked.connect(lambda: self.mw.navigate("模型设置"))
        row.addWidget(self.ai_source_label, 1)
        row.addWidget(open_btn)
        layout.addLayout(row)
        layout.addWidget(hint_label(
            "简历生成/优化、岗位分析（匹配度）、面试准备、薪资谈判等 AI 能力由模型提供："
            "未配置时使用平台默认模型，也可以填入你自己的 API Key 与模型。"
            "配置加密存放在账号下，与网页版共用同一份。"
        ))
        return frame

    # ===== 文件（导出目录） =====
    def _file_card(self) -> QFrame:
        frame = card()
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(12)
        layout.addWidget(section_label("生成文件"))
        row = QHBoxLayout()
        self.export_dir_label = QLabel("")
        self.export_dir_label.setProperty("class", "hint")
        self.export_dir_label.setWordWrap(True)
        browse_btn = QPushButton("选择目录…")
        browse_btn.clicked.connect(self._browse_export_dir)
        row.addWidget(self.export_dir_label, 1)
        row.addWidget(browse_btn)
        layout.addLayout(row)
        layout.addWidget(hint_label("简历导出（Word 等）等生成的文件默认保存到此目录。留空则用应用目录下的 exports 文件夹。"))
        return frame

    # ===== 下载目录（更新安装包等下载落点，尊重用户选择，不再写死盘符） =====
    def _download_card(self) -> QFrame:
        frame = card()
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(12)
        layout.addWidget(section_label("下载目录"))
        row = QHBoxLayout()
        self.download_dir_label = QLabel("")
        self.download_dir_label.setProperty("class", "hint")
        self.download_dir_label.setWordWrap(True)
        browse_btn = QPushButton("选择目录…")
        browse_btn.clicked.connect(self._browse_download_dir)
        row.addWidget(self.download_dir_label, 1)
        row.addWidget(browse_btn)
        layout.addLayout(row)
        layout.addWidget(hint_label(
            "软件更新安装包等下载文件默认保存到此目录。留空则使用系统「下载」文件夹"
            "（会尊重你把它重定向到的盘）；没有 E 盘等盘符缺失的用户也能正常下载。"
        ))
        return frame

    # ===== 数据与隐私（含语料共享开关，对齐网页版）=====
    def _privacy_card(self) -> QFrame:
        frame = card()
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(12)
        layout.addWidget(section_label("数据与隐私"))
        info = QLabel(
            "简历、岗位、投递、面试、薪资谈判等数据均存储在云端，与网页版实时同步，"
            "换电脑登录同一账号即可恢复。"
        )
        info.setWordWrap(True)
        info.setProperty("class", "hint")
        layout.addWidget(info)

        self.consent_check = QCheckBox("贡献语料到共享库")
        self.consent_check.toggled.connect(self._on_consent_toggled)
        layout.addWidget(self.consent_check)
        self.consent_hint = hint_label("")
        self.consent_hint.setWordWrap(True)
        layout.addWidget(self.consent_hint)
        return frame

    def _consent_text(self, allow: bool) -> str:
        return ("已开启：你的简历 / 岗位 / 面经会脱敏处理后用于提升检索质量。"
                if allow else
                "已关闭：你的内容不会进入共享语料库。开启后会脱敏处理，"
                "用于提升大家的检索质量。")

    def _apply_consent(self, allow: bool, err: str = "") -> None:
        """把服务端状态写回界面（用 _consent_loading 抑制 toggled 回调）。"""
        if not hasattr(self, "consent_check"):
            return
        self._consent_loading = True
        self.consent_check.setChecked(bool(allow))
        self._consent_loading = False
        self.consent_check.setEnabled(True)
        self.consent_hint.setText(f"读取失败：{err}" if err else self._consent_text(bool(allow)))

    def _on_consent_toggled(self, checked: bool) -> None:
        if getattr(self, "_consent_loading", False):
            return
        from ...llm import LLMWorker

        self.consent_check.setEnabled(False)
        self.consent_hint.setText("保存中…")
        worker = LLMWorker(api_guard(lambda: api.set_consent(bool(checked))))
        worker.success.connect(lambda val: self._on_consent_saved(True, val, checked))
        worker.failed.connect(lambda err: self._on_consent_saved(False, err, checked))
        worker.start()
        self._consent_worker = worker

    def _on_consent_saved(self, ok: bool, payload, wanted: bool) -> None:
        if ok:
            self._apply_consent(bool(payload))
        else:
            # 保存失败要把开关拨回去，否则界面显示的状态和服务器不一致
            self._consent_loading = True
            self.consent_check.setChecked(not wanted)
            self._consent_loading = False
            self.consent_check.setEnabled(True)
            self.consent_hint.setText(f"保存失败：{payload}")

    def _refresh_consent(self) -> None:
        """后台拉取语料开关（进页面时刷新，配置可能在网页版改过）。"""
        from ...llm import LLMWorker

        if not hasattr(self, "consent_check"):
            return
        self.consent_check.setEnabled(False)
        self.consent_hint.setText("读取中…")
        worker = LLMWorker(api_guard(api.get_consent))
        worker.success.connect(lambda val: self._apply_consent(bool(val)))
        worker.failed.connect(lambda err: self._apply_consent(False, str(err)))
        worker.start()
        self._consent_worker = worker

    # ===== 启动行为 =====
    def _startup_card(self) -> QFrame:
        frame = card()
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(12)
        layout.addWidget(section_label("启动行为"))
        self.auto_start_check = QCheckBox("开机自启动")
        self.auto_start_check.toggled.connect(self._on_auto_start)
        layout.addWidget(self.auto_start_check)
        layout.addWidget(hint_label("关闭窗口不会退出程序，会最小化到系统托盘后台运行；从托盘菜单可彻底退出。"))

        shortcut_row = QHBoxLayout()
        self.shortcut_btn = QPushButton("创建桌面快捷方式")
        self.shortcut_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.shortcut_btn.clicked.connect(self._create_shortcut)
        self.shortcut_hint = hint_label("")
        shortcut_row.addWidget(self.shortcut_btn)
        shortcut_row.addWidget(self.shortcut_hint, 1)
        layout.addLayout(shortcut_row)
        return frame

    def _create_shortcut(self) -> None:
        from ...services.shortcut import create_desktop_shortcut
        ok, msg = create_desktop_shortcut()
        if ok:
            self.shortcut_hint.setText(f"已创建桌面快捷方式（{msg}）")
        else:
            self.shortcut_hint.setText(f"创建失败：{msg}")

    # ===== 加载 / 交互 =====
    def _render_account(self) -> None:
        """账号卡：主行显示昵称，副行显示邮箱（昵称没设时用邮箱兜底）。"""
        u = api.get_user() or {}
        name = str(u.get("name") or "").strip()
        email = str(u.get("email") or "").strip() or (api.get_active_user() or "")
        if name and email and name.lower() != email.lower():
            self.account_name_label.setText(name)
            self.account_hint_label.setText(email)
        else:
            self.account_name_label.setText(name or email or "未登录")
            self.account_hint_label.setText("你的数据与网页版实时同步")
        self._render_avatar()

    def _load(self) -> None:
        self._render_account()

        cfg = db.get_system_config()
        idx = self.theme_combo.findData(cfg.get("theme") or "light")
        if idx < 0:
            # 配置里是个不认识的值：退回「浅色」，**不能**顺手落到下标 0 ——
            # 0 现在是「跟随系统」，那会让用户以为设置被改了。
            idx = self.theme_combo.findData("light")
        self.theme_combo.setCurrentIndex(max(0, idx))
        self.export_dir_label.setText(cfg.get("export_dir") or "（默认：应用目录 exports/）")
        self.download_dir_label.setText(cfg.get("download_dir") or "（默认：系统下载文件夹）")
        self.auto_start_check.setChecked(bool(cfg.get("auto_start")))

    def _on_theme_changed(self) -> None:
        theme = self.theme_combo.currentData()
        db.set_system_config(theme=theme)
        # 统一交给 MainWindow.apply_theme：由它把 "system" 解析成实际明暗，
        # 一次同步 QSS / 手绘色 / 侧栏图标三处。这里自己写一遍的话，
        # 迟早会出现"QSS 变了、图标没变"这种只改一半的情况。
        self.mw.apply_theme(theme)

    def _on_auto_start(self, enabled: bool) -> None:
        ok = _set_auto_start(bool(enabled))
        db.set_system_config(auto_start=bool(enabled) if ok else False)
        if enabled and not ok:
            self.auto_start_check.setChecked(False)

    def _browse_export_dir(self) -> None:
        d = QFileDialog.getExistingDirectory(self, "选择导出文件默认目录", db.resolve_export_dir())
        if d:
            db.set_system_config(export_dir=d)
            self.export_dir_label.setText(d)

    def _browse_download_dir(self) -> None:
        d = QFileDialog.getExistingDirectory(self, "选择下载文件默认目录", db.resolve_download_dir())
        if d:
            db.set_system_config(download_dir=d)
            self.download_dir_label.setText(d)

    # ===== AI 来源刷新 =====
    def showEvent(self, event):
        """每次进入本页刷新账号、AI 来源与语料开关（都可能在网页版改过）。"""
        super().showEvent(event)
        self._render_account()
        self._refresh_ai_source()
        self._refresh_consent()

    def _refresh_ai_source(self) -> None:
        """后台拉取当前 AI 来源，不阻塞界面（要请求云端）。

        不能在工作线程里用 QTimer.singleShot：那个定时器归属于工作线程，
        没有事件循环就永远不会触发（实测确认）。跨线程回主线程要走 Qt 信号。
        """
        from ...llm import LLMWorker

        self.ai_source_label.setText("AI 服务：读取中…")
        worker = LLMWorker(api.describe_ai_source)
        worker.success.connect(
            lambda src: self.ai_source_label.setText(f"AI 服务：{src}")
        )
        worker.failed.connect(
            lambda _msg: self.ai_source_label.setText("AI 服务：获取失败")
        )
        worker.start()
        self._ai_worker = worker  # 持有引用，防止线程对象被 GC
