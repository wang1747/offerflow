"""换绑登录邮箱：新邮箱收验证码 → 提交换绑。

与网页版「设置 → 登录邮箱」的换绑流程对齐，走同一组后端接口：
    POST /api/v1/auth/change-email/send-code   （验证码发往**新邮箱**）
    POST /api/v1/auth/change-email

邮箱是唯一登录账号，所以换绑成功后必须同步更新本机的「记住账号」，
否则下次登录会拿旧邮箱去登，直接失败。
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

# 与网页版 Settings.tsx 的校验规则保持一致
EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")

RESEND_SECONDS = 60


class ChangeEmailDialog(QDialog):
    """换绑邮箱。成功后 `new_email` 记录新邮箱，由调用方刷新界面。"""

    def __init__(self, parent=None, current_email: str = ""):
        super().__init__(parent)
        self.setWindowTitle(APP_NAME)
        # 高度按内容实测：提示条显示时底部仍余 29px，隐藏时约 75px
        self.setFixedSize(400, 400)
        self.setModal(True)
        self.current_email = (current_email or "").strip()
        self.new_email = ""
        self._worker = None
        self._busy = False
        self._countdown = 0

        self._build_ui()

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
        sub = QLabel("换绑邮箱")
        sub.setObjectName("LoginSub")
        sub.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(logo)
        layout.addWidget(sub)
        layout.addSpacing(6)

        tip = hint_label("邮箱是唯一登录账号，需用新邮箱验证码验证")
        tip.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(tip)
        layout.addSpacing(16)

        cur = QLabel(f"当前邮箱：{self.current_email or '未绑定'}")
        cur.setProperty("class", "hint")
        cur.setWordWrap(True)
        layout.addWidget(cur)
        layout.addSpacing(10)

        self.email_edit = QLineEdit()
        self.email_edit.setPlaceholderText("请输入新邮箱")
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

        self.msg = hint_label("")
        self.msg.setWordWrap(True)
        self.msg.hide()
        layout.addWidget(self.msg)
        layout.addSpacing(14)

        self.submit_btn = QPushButton("确认换绑")
        self.submit_btn.setProperty("class", "primary")
        self.submit_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.submit_btn.setMinimumHeight(40)
        self.submit_btn.clicked.connect(self._submit)
        layout.addWidget(self.submit_btn)
        layout.addSpacing(10)

        back_row = QHBoxLayout()
        back_row.addStretch(1)
        cancel_btn = QPushButton("取消")
        cancel_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        cancel_btn.clicked.connect(self.reject)
        back_row.addWidget(cancel_btn)
        back_row.addStretch(1)
        layout.addLayout(back_row)
        layout.addStretch(1)

        self.code_edit.returnPressed.connect(self._submit)
        self.email_edit.returnPressed.connect(self._send_code)
        self.email_edit.setFocus()

    # ==================== 提示 / 状态 ====================

    def _show_msg(self, ok: bool, text: str) -> None:
        # 统一走 components.set_result_message：原先这里写死 #059669/#dc2626，
        # 深色主题下红字对比度仅 3.70:1；且与另外 3 个对话框是同一段复制代码。
        set_result_message(self.msg, ok, text)

    def _set_busy(self, busy: bool, action: str = "") -> None:
        self._busy = busy
        self.submit_btn.setEnabled(not busy)
        self.email_edit.setEnabled(not busy)
        self.code_edit.setEnabled(not busy)
        self.send_btn.setEnabled(not busy and self._countdown <= 0)
        self.submit_btn.setText("换绑中…" if busy and action == "change" else "确认换绑")
        if not busy and self._countdown <= 0:
            self.send_btn.setText("发送验证码")

    # ==================== 发送验证码 ====================

    def _new_email_value(self) -> str:
        return self.email_edit.text().strip()

    def _check_new_email(self) -> str:
        """返回校验通过的新邮箱；不通过则提示并返回空串。"""
        value = self._new_email_value()
        if not EMAIL_RE.match(value):
            self._show_msg(False, "请输入正确的邮箱")
            return ""
        if self.current_email and value.lower() == self.current_email.lower():
            self._show_msg(False, "新邮箱与当前一致")
            return ""
        return value

    def _send_code(self) -> None:
        if self._busy or self._countdown > 0:
            return
        value = self._check_new_email()
        if not value:
            return

        self._set_busy(True, "send")
        self.send_btn.setText("发送中…")
        worker = LLMWorker(api_guard(lambda: api.send_change_email_code(value)))
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

    # ==================== 提交换绑 ====================

    def _submit(self) -> None:
        if self._busy:
            return
        value = self._check_new_email()
        if not value:
            return
        code = self.code_edit.text().strip()
        if not code:
            self._show_msg(False, "请输入邮箱验证码")
            return

        self._set_busy(True, "change")
        worker = LLMWorker(api_guard(lambda: api.change_email(value, code)))
        worker.success.connect(self._on_changed)
        worker.failed.connect(self._on_change_failed)
        worker.start()
        self._worker = worker

    def _on_changed(self, payload) -> None:
        data = payload if isinstance(payload, dict) else {}
        self._set_busy(False)
        self._timer.stop()
        self.new_email = str(data.get("email") or self._new_email_value())
        self._show_msg(True, "邮箱已换绑，请使用新邮箱登录")
        QTimer.singleShot(1200, self.accept)

    def _on_change_failed(self, msg: str) -> None:
        self._set_busy(False)
        self._show_msg(False, str(msg))
