"""登录 / 注册窗口。"""

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox, QDialog, QHBoxLayout, QLabel, QLineEdit, QPushButton, QVBoxLayout, QWidget,
)

from .. import api_client as api
from .. import database as db
from ..config import APP_NAME
from .components import hint_label
from .forgot_password import ForgotPasswordDialog


class LoginDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle(APP_NAME)
        self.setFixedSize(400, 540)
        self.setModal(True)
        self._mode = "login"  # login / register
        self.accepted_user = None

        self._build_ui()
        self._load_remembered()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(40, 36, 40, 32)
        layout.setSpacing(0)

        # Logo
        logo = QLabel("OfferFlow")
        logo.setObjectName("LoginLogo")
        logo.setAlignment(Qt.AlignmentFlag.AlignCenter)
        sub = QLabel("求职全流程智能工作台")
        sub.setObjectName("LoginSub")
        sub.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(logo)
        layout.addWidget(sub)
        layout.addSpacing(28)

        # 表单
        self.user_edit = QLineEdit()
        self.user_edit.setPlaceholderText("邮箱")
        self.pwd_edit = QLineEdit()
        self.pwd_edit.setPlaceholderText("密码（至少 6 位）")
        self.pwd_edit.setEchoMode(QLineEdit.EchoMode.Password)
        layout.addWidget(self.user_edit)
        layout.addSpacing(12)
        layout.addWidget(self.pwd_edit)
        layout.addSpacing(10)

        # 记住账号 / 记住密码
        remember_row = QHBoxLayout()
        remember_row.setSpacing(16)
        self.remember_user_check = QCheckBox("记住账号")
        self.remember_pwd_check = QCheckBox("记住密码")
        self.remember_pwd_check.toggled.connect(
            lambda on: self.remember_user_check.setChecked(True) if on else None
        )
        remember_row.addWidget(self.remember_user_check)
        remember_row.addWidget(self.remember_pwd_check)
        remember_row.addStretch(1)
        # 忘记密码（同网页版 ForgotPassword 入口）
        self.forgot_btn = QPushButton("忘记密码？")
        self.forgot_btn.setObjectName("LinkButton")
        self.forgot_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.forgot_btn.setFlat(True)
        self.forgot_btn.setStyleSheet(
            "QPushButton#LinkButton { border: none; background: transparent;"
            " color: #4f46e5; font-size: 12px; padding: 0; }"
            "QPushButton#LinkButton:hover { color: #4338ca; }"
        )
        self.forgot_btn.clicked.connect(self._open_forgot)
        remember_row.addWidget(self.forgot_btn)
        layout.addLayout(remember_row)
        layout.addSpacing(10)

        self.hint = hint_label("")
        self.hint.setAlignment(Qt.AlignmentFlag.AlignLeft)
        layout.addWidget(self.hint)
        layout.addSpacing(16)

        self.submit_btn = QPushButton("登录")
        self.submit_btn.setProperty("class", "primary")
        self.submit_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.submit_btn.setMinimumHeight(40)
        self.submit_btn.clicked.connect(self._submit)
        layout.addWidget(self.submit_btn)
        layout.addSpacing(12)

        switch_row = QHBoxLayout()
        switch_row.addStretch(1)
        self.switch_label = QLabel("没有账号？")
        self.switch_label.setObjectName("LoginSwitchHint")
        self.switch_btn = QPushButton("注册")
        self.switch_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.switch_btn.clicked.connect(self._toggle_mode)
        switch_row.addWidget(self.switch_label)
        switch_row.addWidget(self.switch_btn)
        switch_row.addStretch(1)
        layout.addLayout(switch_row)
        layout.addStretch(1)

        self.pwd_edit.returnPressed.connect(self._submit)
        self.user_edit.returnPressed.connect(lambda: self.pwd_edit.setFocus())

        # 默认聚焦用户名输入框
        self.user_edit.setFocus()

    def _load_remembered(self) -> None:
        """启动时回填记住的账号 / 密码。"""
        username, password = db.get_remembered_login()
        if username:
            self.user_edit.setText(username)
            self.remember_user_check.setChecked(True)
        if password:
            self.pwd_edit.setText(password)
            self.remember_pwd_check.setChecked(True)
            self.remember_user_check.setChecked(True)

    def _toggle_mode(self) -> None:
        if self._mode == "login":
            self._mode = "register"
            self.submit_btn.setText("注册")
            self.switch_label.setText("已有账号？")
            self.switch_btn.setText("登录")
            self.hint.setText("")
        else:
            self._mode = "login"
            self.submit_btn.setText("登录")
            self.switch_label.setText("没有账号？")
            self.switch_btn.setText("注册")
            self.hint.setText("")

    def _open_forgot(self) -> None:
        """打开找回密码窗口；重置成功后回填邮箱，让用户直接登一次。"""
        dlg = ForgotPasswordDialog(self, account=self.user_edit.text().strip())
        if dlg.exec() == QDialog.DialogCode.Accepted and dlg.reset_account:
            if self._mode != "login":
                self._toggle_mode()
            self.user_edit.setText(dlg.reset_account)
            self.pwd_edit.clear()
            self.pwd_edit.setFocus()
            self.hint.setText("密码已重置，请使用新密码登录")

    def _submit(self) -> None:
        username = self.user_edit.text().strip()
        password = self.pwd_edit.text()
        if self._mode == "login":
            ok, msg = api.login(username, password)
            if ok:
                db.save_remembered_login(
                    username, password,
                    remember_user=self.remember_user_check.isChecked(),
                    remember_pwd=self.remember_pwd_check.isChecked(),
                )
                self.accepted_user = api.get_active_user() or username
                self.accept()
            else:
                self.hint.setText(msg)
        else:
            ok, msg = api.register(username, password)
            if ok:
                self.hint.setText("注册成功，请登录")
                self._toggle_mode()
                self.user_edit.setText(username)
                self.pwd_edit.clear()
            else:
                self.hint.setText(msg)
