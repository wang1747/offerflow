"""找回密码窗口：邮箱验证码 → 重置新密码。

与网页版 `ForgotPassword.tsx` 对齐，走同一组后端接口：
    POST /api/v1/auth/forgot-password/send-code
    POST /api/v1/auth/forgot-password/reset

两个请求都发邮件 / 走网络，必须放后台线程，否则窗口会假死。
"""

import re

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QDialog, QHBoxLayout, QLabel, QLineEdit, QPushButton, QVBoxLayout,
)

from .. import api_client as api
from ..config import APP_NAME
from ..llm import LLMWorker
from .components import api_guard, hint_label, set_result_message

# 与网页版 ForgotPassword.tsx 里的 EMAIL_RE 保持一致
EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")

# 重发间隔（秒），与后端 _CODE_RESEND_INTERVAL 对齐
RESEND_SECONDS = 60


class ForgotPasswordDialog(QDialog):
    """找回密码。成功重置后 `reset_account` 记录邮箱，供登录页回填。"""

    def __init__(self, parent=None, account: str = ""):
        super().__init__(parent)
        self.setWindowTitle(APP_NAME)
        # 高度按内容量定：480 刚好在「提示条显示」时不挤压按钮、也不留大片空白
        self.setFixedSize(400, 480)
        self.setModal(True)
        self.reset_account = ""
        self._worker = None
        self._busy = False
        self._countdown = 0

        self._build_ui()

        if account:
            self.email_edit.setText(account)
            self.code_edit.setFocus()
        else:
            self.email_edit.setFocus()

        self._timer = QTimer(self)
        self._timer.setInterval(1000)
        self._timer.timeout.connect(self._tick)

    # ==================== 界面 ====================

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(40, 32, 40, 28)
        layout.setSpacing(0)

        logo = QLabel("OfferFlow")
        logo.setObjectName("LoginLogo")
        logo.setAlignment(Qt.AlignmentFlag.AlignCenter)
        sub = QLabel("找回密码")
        sub.setObjectName("LoginSub")
        sub.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(logo)
        layout.addWidget(sub)
        layout.addSpacing(6)

        tip = hint_label("通过邮箱验证码重置密码")
        tip.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(tip)
        layout.addSpacing(20)

        self.email_edit = QLineEdit()
        self.email_edit.setPlaceholderText("注册邮箱")
        layout.addWidget(self.email_edit)
        layout.addSpacing(10)

        code_row = QHBoxLayout()
        code_row.setSpacing(8)
        self.code_edit = QLineEdit()
        self.code_edit.setPlaceholderText("6 位验证码")
        self.code_edit.setMaxLength(6)
        self.send_btn = QPushButton("发送验证码")
        self.send_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.send_btn.setMinimumHeight(34)
        self.send_btn.clicked.connect(self._send_code)
        code_row.addWidget(self.code_edit, 1)
        code_row.addWidget(self.send_btn)
        layout.addLayout(code_row)
        layout.addSpacing(10)

        self.pwd_edit = QLineEdit()
        self.pwd_edit.setPlaceholderText("新密码（至少 6 位）")
        self.pwd_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.pwd_edit.setMaxLength(128)
        layout.addWidget(self.pwd_edit)
        layout.addSpacing(10)

        self.pwd2_edit = QLineEdit()
        self.pwd2_edit.setPlaceholderText("确认新密码")
        self.pwd2_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.pwd2_edit.setMaxLength(128)
        layout.addWidget(self.pwd2_edit)
        layout.addSpacing(10)

        self.msg = hint_label("")
        self.msg.setWordWrap(True)
        self.msg.hide()
        layout.addWidget(self.msg)
        layout.addSpacing(14)

        self.submit_btn = QPushButton("重置密码")
        self.submit_btn.setProperty("class", "primary")
        self.submit_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.submit_btn.setMinimumHeight(40)
        self.submit_btn.clicked.connect(self._submit)
        layout.addWidget(self.submit_btn)
        layout.addSpacing(10)

        back_row = QHBoxLayout()
        back_row.addStretch(1)
        back_hint = QLabel("想起密码了？")
        back_hint.setObjectName("LoginSwitchHint")
        back_btn = QPushButton("返回登录")
        back_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        back_btn.clicked.connect(self.reject)
        back_row.addWidget(back_hint)
        back_row.addWidget(back_btn)
        back_row.addStretch(1)
        layout.addLayout(back_row)
        layout.addStretch(1)

        self.pwd2_edit.returnPressed.connect(self._submit)
        self.email_edit.returnPressed.connect(self._send_code)

    # ==================== 提示 ====================

    def _show_msg(self, ok: bool, text: str) -> None:
        set_result_message(self.msg, ok, text)

    def _set_busy(self, busy: bool, action: str = "") -> None:
        self._busy = busy
        self.submit_btn.setEnabled(not busy)
        self.email_edit.setEnabled(not busy)
        self.code_edit.setEnabled(not busy)
        self.pwd_edit.setEnabled(not busy)
        self.pwd2_edit.setEnabled(not busy)
        # 倒计时未结束时发送按钮保持不可点
        self.send_btn.setEnabled(not busy and self._countdown <= 0)
        self.submit_btn.setText("提交中…" if busy and action == "reset" else "重置密码")
        if not busy and self._countdown <= 0:
            self.send_btn.setText("发送验证码")

    # ==================== 发送验证码 ====================

    def _send_code(self) -> None:
        if self._busy or self._countdown > 0:
            return
        account = self.email_edit.text().strip()
        if not EMAIL_RE.match(account):
            self._show_msg(False, "请输入正确的邮箱")
            self.email_edit.setFocus()
            return

        self._set_busy(True, "send")
        self.send_btn.setText("发送中…")
        worker = LLMWorker(api_guard(lambda: api.send_reset_code(account)))
        worker.success.connect(self._on_code_sent)
        worker.failed.connect(self._on_send_failed)
        worker.start()
        self._worker = worker

    def _on_code_sent(self, payload) -> None:
        data = payload if isinstance(payload, dict) else {}
        masked = data.get("email_masked") or ""
        self._set_busy(False)
        self._show_msg(True, f"验证码已发送至 {masked}" if masked else "验证码已发送")
        self._start_countdown()
        self.code_edit.setFocus()

    def _on_send_failed(self, msg: str) -> None:
        self._set_busy(False)
        self._show_msg(False, str(msg))

    def _start_countdown(self) -> None:
        self._countdown = RESEND_SECONDS
        self.send_btn.setEnabled(False)
        self.send_btn.setText(f"{self._countdown}s")
        self._timer.start()

    def _tick(self) -> None:
        self._countdown -= 1
        if self._countdown <= 0:
            self._countdown = 0
            self._timer.stop()
            self.send_btn.setText("发送验证码")
            self.send_btn.setEnabled(not self._busy)
            return
        self.send_btn.setText(f"{self._countdown}s")

    # ==================== 重置密码 ====================

    def _submit(self) -> None:
        if self._busy:
            return
        account = self.email_edit.text().strip()
        code = self.code_edit.text().strip()
        pwd = self.pwd_edit.text()
        pwd2 = self.pwd2_edit.text()

        if not EMAIL_RE.match(account):
            self._show_msg(False, "请输入正确的邮箱")
            return
        if not code:
            self._show_msg(False, "请输入验证码")
            return
        if len(pwd) < 6:
            self._show_msg(False, "新密码至少 6 位")
            return
        if pwd != pwd2:
            self._show_msg(False, "两次密码输入不一致")
            return

        self._set_busy(True, "reset")
        worker = LLMWorker(api_guard(
            lambda: api.reset_password(account, code, pwd, email=account)
        ))
        worker.success.connect(self._on_reset_done)
        worker.failed.connect(self._on_reset_failed)
        worker.start()
        self._worker = worker

    def _on_reset_done(self, payload) -> None:
        data = payload if isinstance(payload, dict) else {}
        self._set_busy(False)
        self.reset_account = self.email_edit.text().strip()
        self._show_msg(True, data.get("message") or "密码已重置，请使用新密码登录")
        self._timer.stop()
        # 让用户看清提示再自动回到登录页
        QTimer.singleShot(1200, self.accept)

    def _on_reset_failed(self, msg: str) -> None:
        self._set_busy(False)
        self._show_msg(False, str(msg))
