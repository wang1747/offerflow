"""个人资料与账号安全对话框：修改昵称、修改登录密码。

对齐网页版「设置 → 账号与偏好」与「管理后台 → 账号安全」，走同一组后端接口：
    PATCH /api/v1/auth/profile           （昵称，1~32 字）
    POST  /api/v1/auth/change-password   （旧密码校验 → 新密码 bcrypt 存储）
"""

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog, QHBoxLayout, QLabel, QLineEdit, QPushButton, QVBoxLayout,
)

from .. import api_client as api
from ..config import APP_NAME
from ..llm import LLMWorker
from .components import api_guard, hint_label, set_result_message


class _BaseDialog(QDialog):
    """两个资料类对话框共用的骨架（标题、提示条、忙碌态、异步调用）。"""

    def __init__(self, parent=None, subtitle: str = "", tip: str = ""):
        super().__init__(parent)
        self.setWindowTitle(APP_NAME)
        self.setModal(True)
        self._worker = None
        self._busy = False
        self._subtitle = subtitle
        self._tip = tip

    # ---------- 骨架 ----------
    def _build_head(self, layout: QVBoxLayout) -> None:
        logo = QLabel("OfferFlow")
        logo.setObjectName("LoginLogo")
        logo.setAlignment(Qt.AlignmentFlag.AlignCenter)
        sub = QLabel(self._subtitle)
        sub.setObjectName("LoginSub")
        sub.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(logo)
        layout.addWidget(sub)
        layout.addSpacing(6)
        if self._tip:
            tip = hint_label(self._tip)
            tip.setAlignment(Qt.AlignmentFlag.AlignCenter)
            layout.addWidget(tip)
            layout.addSpacing(14)

    def _build_msg(self, layout: QVBoxLayout) -> None:
        self.msg = hint_label("")
        self.msg.setWordWrap(True)
        self.msg.hide()
        layout.addWidget(self.msg)

    def _build_footer(self, layout: QVBoxLayout, submit_text: str) -> None:
        layout.addSpacing(14)
        self.submit_btn = QPushButton(submit_text)
        self.submit_btn.setProperty("class", "primary")
        self.submit_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.submit_btn.setMinimumHeight(40)
        self.submit_btn.clicked.connect(self._submit)
        layout.addWidget(self.submit_btn)

        layout.addSpacing(10)
        row = QHBoxLayout()
        row.addStretch(1)
        cancel_btn = QPushButton("取消")
        cancel_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        cancel_btn.clicked.connect(self.reject)
        row.addWidget(cancel_btn)
        row.addStretch(1)
        layout.addLayout(row)
        layout.addStretch(1)

    # ---------- 提示 / 忙碌 ----------
    def _show_msg(self, ok: bool, text: str) -> None:
        set_result_message(self.msg, ok, text)

    def _set_busy(self, busy: bool, busy_text: str, idle_text: str) -> None:
        self._busy = busy
        self.submit_btn.setEnabled(not busy)
        self.submit_btn.setText(busy_text if busy else idle_text)
        for w in self.inputs():
            w.setEnabled(not busy)

    def inputs(self):
        """子类返回需要在忙碌时禁用的输入控件。"""
        return []

    def _run(self, fn, ok_cb) -> None:
        """把请求丢到后台线程（跨线程回主线程必须走信号，不能用 QTimer.singleShot）。"""
        self.msg.hide()
        worker = LLMWorker(api_guard(fn))
        worker.success.connect(lambda payload: self._done(True, payload, ok_cb))
        worker.failed.connect(lambda err: self._done(False, err, ok_cb))
        worker.start()
        self._worker = worker

    def _done(self, ok: bool, payload, ok_cb) -> None:
        self._set_busy(False, "", self._idle_text)
        if ok:
            ok_cb(payload)
        else:
            self._show_msg(False, str(payload))

    def _submit(self) -> None:
        raise NotImplementedError


class NicknameDialog(_BaseDialog):
    """修改昵称。成功后 `new_name` 是新昵称。"""

    def __init__(self, parent=None, current_name: str = ""):
        super().__init__(parent, subtitle="修改昵称", tip="昵称会显示在交流中心与反馈里")
        self.current_name = (current_name or "").strip()
        self.new_name = ""
        self._idle_text = "保存"

        layout = QVBoxLayout(self)
        layout.setContentsMargins(40, 32, 40, 28)
        layout.setSpacing(0)
        self._build_head(layout)

        cur = QLabel(f"当前昵称：{self.current_name or '（未设置）'}")
        cur.setProperty("class", "hint")
        cur.setWordWrap(True)
        layout.addWidget(cur)
        layout.addSpacing(10)

        self.name_edit = QLineEdit()
        self.name_edit.setPlaceholderText("请输入昵称（1~32 字）")
        self.name_edit.setMaxLength(32)
        self.name_edit.setText(self.current_name)
        layout.addWidget(self.name_edit)
        layout.addSpacing(8)

        self._build_msg(layout)
        self._build_footer(layout, "保存")
        self.name_edit.returnPressed.connect(self._submit)
        self.setFixedSize(400, 400)
        self.name_edit.selectAll()
        self.name_edit.setFocus()

    def inputs(self):
        return [self.name_edit]

    def _submit(self) -> None:
        if self._busy:
            return
        value = self.name_edit.text().strip()
        # 本地先拦一道，避免直接吃服务端 422
        if not value:
            self._show_msg(False, "昵称不能为空")
            return
        if value == self.current_name:
            self._show_msg(False, "昵称未变化")
            return
        self._set_busy(True, "保存中…", "保存")
        self._run(lambda: api.update_profile(value), self._on_ok)

    def _on_ok(self, payload) -> None:
        self.new_name = (payload or {}).get("name") or self.name_edit.text().strip()
        self.accept()


class ChangePasswordDialog(_BaseDialog):
    """修改登录密码。成功后 `changed` 为 True。"""

    def __init__(self, parent=None):
        super().__init__(parent, subtitle="修改密码",
                         tip="修改后当前登录仍然有效，下次登录请用新密码")
        self.changed = False
        self._idle_text = "确认修改"

        layout = QVBoxLayout(self)
        layout.setContentsMargins(40, 32, 40, 28)
        layout.setSpacing(0)
        self._build_head(layout)

        self.old_edit = QLineEdit()
        self.old_edit.setPlaceholderText("当前密码")
        self.old_edit.setEchoMode(QLineEdit.EchoMode.Password)
        layout.addWidget(self.old_edit)
        layout.addSpacing(10)

        self.new_edit = QLineEdit()
        self.new_edit.setPlaceholderText("新密码（至少 6 位）")
        self.new_edit.setEchoMode(QLineEdit.EchoMode.Password)
        layout.addWidget(self.new_edit)
        layout.addSpacing(10)

        self.confirm_edit = QLineEdit()
        self.confirm_edit.setPlaceholderText("再次输入新密码")
        self.confirm_edit.setEchoMode(QLineEdit.EchoMode.Password)
        layout.addWidget(self.confirm_edit)
        layout.addSpacing(8)

        self._build_msg(layout)
        self._build_footer(layout, "确认修改")
        self.confirm_edit.returnPressed.connect(self._submit)
        self.setFixedSize(400, 460)
        self.old_edit.setFocus()

    def inputs(self):
        return [self.old_edit, self.new_edit, self.confirm_edit]

    def _submit(self) -> None:
        if self._busy:
            return
        old = self.old_edit.text()
        new = self.new_edit.text()
        confirm = self.confirm_edit.text()
        if not old:
            self._show_msg(False, "请输入当前密码")
            return
        if len(new) < 6:
            self._show_msg(False, "新密码至少 6 位")
            return
        if new != confirm:
            self._show_msg(False, "两次输入的新密码不一致")
            return
        if new == old:
            self._show_msg(False, "新密码不能与当前密码相同")
            return
        self._set_busy(True, "提交中…", "确认修改")
        self._run(lambda: api.change_password(old, new), self._on_ok)

    def _on_ok(self, payload) -> None:
        self.changed = True
        self._show_msg(True, str(payload or "密码已修改"))
        self.submit_btn.setText("已完成")
        self.submit_btn.setEnabled(False)
