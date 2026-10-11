"""模型设置页：自带 API Key（BYOK）。

对齐网页版 `ModelSettings.tsx`，也补齐桌面版原先的缺口 —— 之前桌面版
只能被动用云端出 Key，设置页还写着「无需在本机配置 API Key」。

机制（与网页版完全同一套，同账号共用一份服务端配置）：
- 未配置：服务端用平台默认 Key 兜底（`using_default`）；
- 配置后：服务端改用你自己的 Key 调模型；
- 清除后：回落平台默认。

数据与 AI 依旧全部走服务器数据库，变的只是「这把 Key 是谁的」。
桌面版只提交 Key、只读掩码，明文不回传本机。

网络请求一律走 LLMWorker 后台线程，避免测试连接（最长 60s）卡死界面。
"""

from typing import Any, Dict, List, Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QButtonGroup, QFormLayout, QFrame, QHBoxLayout, QLabel, QLineEdit,
    QMessageBox, QPushButton, QScrollArea, QSizePolicy, QVBoxLayout, QWidget,
)

from ... import api_client as api
from ... import database as local_db
from ...llm import LLMWorker
from ..components import (
    api_guard, card, hint_label, make_title, primary_button, section_label,
    set_result_message, soft_chip,
)

_URL_PREFIXES = ("http://", "https://")


def _clear_layout(box) -> None:
    """清空布局里的所有控件（含 spacer）。"""
    while box.count():
        item = box.takeAt(0)
        w = item.widget()
        if w is not None:
            w.setParent(None)
            w.deleteLater()


def _bold(text: str) -> QLabel:
    """小标题：与主题里的 section-title 同级（用 px 而非 QFont pt，避免 DPI 漂移）。"""
    label = QLabel(text)
    label.setStyleSheet("font-size: 14px; font-weight: 700;")
    return label


def _transparent(widget: QWidget, name: str) -> QWidget:
    """把卡片内的裸容器变成透明。

    主题里有 `QMainWindow, QWidget { background: #f7f8fa; }`，而卡片是白底，
    所以卡片里的裸 QWidget 会显出一块浅灰补丁。必须用 objectName 精准覆盖：
    直接 setStyleSheet("background: transparent") 会连它内部的按钮一起改掉。
    """
    widget.setObjectName(name)
    widget.setStyleSheet(f"#{name} {{ background: transparent; }}")
    return widget


def _sync_local(fields: Dict[str, str]) -> str:
    """把这次保存的配置**同步写一份到本地**，返回给人看的补充说明。

    为什么必须双写：本地 AI（抽取图谱 / 提炼记忆 / `llm.chat`）直连模型，
    读的是本地 SQLite 那份（`database.get_local_model_config`），而云端
    `/api/v1/user/model-config/` **只回掩码、拿不回明文 Key**，本地无法反推。
    早先只写云端 ⇒ 本地那份恒为空 ⇒ 怎么配都提示「未配置模型」（错题本 E-08）。

    写失败不抛异常：云端已经保存成功，不该被本地副本连坐（但如实回报）。
    """
    try:
        local_db.save_local_model_config(
            fields.get("provider", ""), fields["base_url"],
            fields["model"], fields["api_key"],
        )
        return ""
    except Exception as exc:  # noqa: BLE001
        return f"（本地知识库副本写入失败：{type(exc).__name__}: {exc}）"


class ModelSettingsPage(QWidget):
    """模型设置（自有 API Key）。"""

    def __init__(self, main_window):
        super().__init__()
        self.mw = main_window
        self._worker: Optional[LLMWorker] = None
        self._provider_group: Optional[QButtonGroup] = None

        self._presets: List[Dict[str, str]] = []
        self._config: Dict[str, Any] = {}
        self._provider = ""
        self._busy = False

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        content = QWidget()
        layout = QVBoxLayout(content)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.setSpacing(16)
        layout.addWidget(make_title(
            "模型设置",
            "用自己的 API Key 调用 AI 模型。配置存在你的账号下（服务端加密），"
            "桌面版与网页版共用同一份，改一处两端同时生效。",
        ))
        layout.addWidget(self._build_status_card())
        layout.addWidget(self._build_notice_card())
        layout.addWidget(self._build_form_card())
        layout.addStretch(1)

        scroll.setWidget(content)
        outer.addWidget(scroll)

    # ==================== 卡片构建 ====================

    def _build_status_card(self) -> QFrame:
        """当前 AI 来源（动态重绘：已配置 / 平台默认 / 无可用）。"""
        self.status_card = card()
        self.status_card.setSizePolicy(
            QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Preferred
        )
        layout = QVBoxLayout(self.status_card)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(8)
        layout.addWidget(section_label("当前 AI 来源"))
        self.status_body = QVBoxLayout()
        self.status_body.setSpacing(8)
        layout.addLayout(self.status_body)
        return self.status_card

    def _build_notice_card(self) -> QFrame:
        frame = card()
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(8)
        layout.addWidget(section_label("关于密钥"))
        for text in (
            "密钥仅加密存储在你的账号下，只用于调用你指定的模型；桌面版只提交、不回显明文。",
            "平台不代付费用：用自有 Key 时，调用成本完全由你自己掌控。",
            "未配置时自动使用平台默认模型；保存后立即切换为你的模型，清除即回落到默认。",
            "服务端会拒绝 localhost / 内网地址（防 SSRF），因此本机部署的模型需要可公网访问。",
        ):
            layout.addWidget(hint_label("·　" + text))
        return frame

    def _build_form_card(self) -> QFrame:
        frame = card()
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(12)
        layout.addWidget(section_label("配置模型"))

        # 提供商按钮组（预设，点击自动填充 Base URL 与模型名）
        prov_row = QHBoxLayout()
        prov_row.setSpacing(12)
        prov_label = QLabel("模型提供商")
        prov_label.setFixedWidth(88)
        prov_row.addWidget(prov_label)
        self.provider_box = _transparent(QWidget(), "ProviderChipBox")
        self.provider_layout = QHBoxLayout(self.provider_box)
        self.provider_layout.setContentsMargins(0, 0, 0, 0)
        self.provider_layout.setSpacing(8)
        prov_row.addWidget(self.provider_box, 1)
        layout.addLayout(prov_row)

        form = QFormLayout()
        form.setSpacing(12)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignLeft)
        self.base_url_edit = QLineEdit()
        self.base_url_edit.setPlaceholderText("https://api.deepseek.com")
        self.model_edit = QLineEdit()
        self.model_edit.setPlaceholderText("deepseek-chat")
        self.key_edit = QLineEdit()
        self.key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.key_edit.setPlaceholderText("粘贴你的 API Key（保存后只显示掩码）")
        form.addRow("Base URL", self.base_url_edit)
        form.addRow("模型名", self.model_edit)
        form.addRow("API Key", self.key_edit)
        layout.addLayout(form)

        self.masked_label = hint_label("")
        layout.addWidget(self.masked_label)

        btn_row = QHBoxLayout()
        btn_row.setSpacing(8)
        self.test_btn = QPushButton("测试连接")
        self.test_btn.setProperty("class", "ghost")
        self.test_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.test_btn.clicked.connect(self._on_test)
        self.save_btn = primary_button("保存配置")
        self.save_btn.clicked.connect(self._on_save)
        self.clear_btn = QPushButton("清除配置")
        self.clear_btn.setProperty("class", "danger")
        self.clear_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.clear_btn.clicked.connect(self._on_clear)
        btn_row.addWidget(self.test_btn)
        btn_row.addWidget(self.save_btn)
        btn_row.addWidget(self.clear_btn)
        btn_row.addStretch(1)
        layout.addLayout(btn_row)

        self.result_label = QLabel("")
        self.result_label.setWordWrap(True)
        self.result_label.hide()
        layout.addWidget(self.result_label)
        return frame

    # ==================== 状态渲染 ====================

    def _set_status_class(self, cls: str) -> None:
        """动态换卡片配色类（Qt 需要 unpolish/polish 才会重算样式）。"""
        if self.status_card.property("class") == cls:
            return
        self.status_card.setProperty("class", cls)
        style = self.status_card.style()
        style.unpolish(self.status_card)
        style.polish(self.status_card)

    def _render_status(self, cfg: Dict[str, Any]) -> None:
        _clear_layout(self.status_body)

        if not cfg.get("ok"):
            self._set_status_class("card-warn")
            self.status_body.addWidget(_bold("未能读取模型配置"))
            self.status_body.addWidget(hint_label(
                "可能是网络问题。可在下方直接填写并保存，保存时会重新校验。"
            ))
            return

        if cfg.get("has_config"):
            self._set_status_class("card-success")
            row = QHBoxLayout()
            row.setSpacing(8)
            row.addWidget(_bold("已配置自有 Key"))
            provider = cfg.get("provider") or ""
            row.addWidget(soft_chip(
                api.PROVIDER_LABELS.get(provider) or provider or "自有", "#059669"
            ))
            if cfg.get("model"):
                row.addWidget(soft_chip(cfg["model"], "#1d4ed8"))
            row.addStretch(1)
            holder = _transparent(QWidget(), "StatusProviderRow")
            holder.setLayout(row)
            self.status_body.addWidget(holder)
            self.status_body.addWidget(hint_label(
                f"API Key：{cfg.get('api_key_masked') or '—'}"
                f"　·　Base URL：{cfg.get('base_url') or '—'}"
            ))
            self.status_body.addWidget(hint_label(
                "这份配置存在你的账号下（与网页版共用）。本地知识库的「抽取图谱 / "
                "提炼记忆」要在本机直连模型，需要明文 Key —— 在本页保存一次即可"
                "同步一份到本机；只在网页版填过是不会同步过来的。"
            ))
            return

        if cfg.get("using_default"):
            self._set_status_class("card-highlight")
            self.status_body.addWidget(_bold("正在使用平台默认模型"))
            self.status_body.addWidget(hint_label(
                "当前未配置自有 Key，平台已提供默认模型供你使用，"
                "AI 功能可正常调用。如需用自己的模型，在下方填写即可。"
            ))
            return

        self._set_status_class("card-warn")
        self.status_body.addWidget(_bold("未配置，AI 功能不可用"))
        self.status_body.addWidget(hint_label(
            "当前账号既没有自有 Key，平台默认模型也不可用。请在下方配置后使用。"
        ))

    def _show_result(self, ok: bool, msg: str) -> None:
        set_result_message(self.result_label, ok, msg)

    def _set_busy(self, busy: bool, action: str = "") -> None:
        self._busy = busy
        for btn in (self.test_btn, self.save_btn, self.clear_btn):
            btn.setEnabled(not busy)
        if busy:
            if action == "test":
                self.test_btn.setText("测试中…")
            elif action == "save":
                self.save_btn.setText("保存中…")
            elif action == "clear":
                self.clear_btn.setText("清除中…")
        else:
            self.test_btn.setText("测试连接")
            self.save_btn.setText("保存配置")
            self.clear_btn.setText("清除配置")

    # ==================== 数据加载 ====================

    def showEvent(self, event):
        """每次切到本页都刷一次 —— 这样在网页版改过的配置，切回来即可见。"""
        super().showEvent(event)
        self.reload()

    def reload(self) -> None:
        if self._busy:
            return
        self._set_busy(True)

        def do():
            return {"presets": api.get_model_presets(), "config": api.get_model_config()}

        worker = LLMWorker(api_guard(do))
        worker.success.connect(self._on_loaded)
        worker.failed.connect(self._on_load_failed)
        worker.start()
        self._worker = worker

    def _on_load_failed(self, msg: str) -> None:
        self._set_busy(False)
        self._render_status({})
        self._show_result(False, f"读取配置失败：{msg}")

    def _on_loaded(self, payload: Dict[str, Any]) -> None:
        self._set_busy(False)
        self._presets = payload.get("presets") or []
        cfg = payload.get("config") or {}
        self._config = cfg

        self._rebuild_providers()

        # 只在用户没在输入时回填，避免覆盖正在编辑的内容
        if cfg.get("has_config"):
            self._provider = cfg.get("provider") or ""
            if not self.base_url_edit.text().strip():
                self.base_url_edit.setText(cfg.get("base_url") or "")
            if not self.model_edit.text().strip():
                self.model_edit.setText(cfg.get("model") or "")
        self._sync_provider_checked()

        if cfg.get("has_config"):
            self.masked_label.setText(f"当前已存密钥：{cfg.get('api_key_masked') or '—'}")
        elif cfg.get("using_default"):
            self.masked_label.setText("尚未配置自有密钥，当前使用平台默认模型。")
        else:
            self.masked_label.setText("尚未配置自有密钥。")

        self._render_status(cfg)

    # ==================== 提供商按钮组 ====================

    def _rebuild_providers(self) -> None:
        if self._provider_group is not None:
            for btn in self._provider_group.buttons():
                self._provider_group.removeButton(btn)
            self._provider_group.deleteLater()
            self._provider_group = None

        _clear_layout(self.provider_layout)

        group = QButtonGroup(self)
        group.setExclusive(True)
        for idx, preset in enumerate(self._presets):
            btn = QPushButton(preset.get("label") or preset.get("key") or "")
            btn.setProperty("class", "chip")
            btn.setCheckable(True)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            group.addButton(btn, idx)
            self.provider_layout.addWidget(btn)
        self.provider_layout.addStretch(1)
        group.idClicked.connect(self._on_provider_clicked)
        self._provider_group = group

    def _sync_provider_checked(self) -> None:
        if self._provider_group is None:
            return
        for idx, preset in enumerate(self._presets):
            if preset.get("key") == self._provider:
                btn = self._provider_group.button(idx)
                if btn is not None:
                    btn.setChecked(True)
                return

    def _on_provider_clicked(self, idx: int) -> None:
        if not (0 <= idx < len(self._presets)):
            return
        preset = self._presets[idx]
        self._provider = preset.get("key") or ""
        # 预设里 qwen / glm 的 base_url 与 model 尚未确定（服务端留空），
        # 只填非空字段，避免把用户已填的内容抹掉。
        if preset.get("base_url"):
            self.base_url_edit.setText(preset["base_url"])
        if preset.get("model"):
            self.model_edit.setText(preset["model"])

    # ==================== 表单校验与操作 ====================

    def _collect(self, require_key: bool = True) -> Optional[Dict[str, str]]:
        base_url = self.base_url_edit.text().strip()
        model = self.model_edit.text().strip()
        api_key = self.key_edit.text().strip()

        if not self._provider:
            self._show_result(False, "请先选择模型提供商。")
            return None
        if not base_url or not base_url.lower().startswith(_URL_PREFIXES):
            self._show_result(False, "Base URL 需要以 http:// 或 https:// 开头。")
            return None
        if not model:
            self._show_result(False, "请填写模型名。")
            return None
        if require_key:
            if not api_key:
                self._show_result(False, "请填写 API Key。")
                return None
            if len(api_key) < api.MODEL_KEY_MIN_LENGTH:
                self._show_result(
                    False, f"API Key 长度至少 {api.MODEL_KEY_MIN_LENGTH} 位。"
                )
                return None
        return {
            "provider": self._provider,
            "base_url": base_url,
            "model": model,
            "api_key": api_key,
        }

    def _run(self, action: str, fn, ok_cb) -> None:
        """统一把耗时请求丢到后台线程，并接管按钮 busy 态。"""
        self._set_busy(True, action)
        self.result_label.hide()
        worker = LLMWorker(api_guard(fn))
        worker.success.connect(lambda payload: self._on_done(True, payload, ok_cb))
        worker.failed.connect(lambda msg: self._on_done(False, msg, ok_cb))
        worker.start()
        self._worker = worker

    def _on_done(self, ok: bool, payload, ok_cb) -> None:
        self._set_busy(False)
        if ok:
            ok_cb(payload)
        else:
            self._show_result(False, str(payload))

    def _on_test(self) -> None:
        fields = self._collect(require_key=True)
        if fields is None:
            return
        self._run(
            "test",
            lambda: api.test_model_connection(
                fields["base_url"], fields["api_key"], fields["model"]
            ),
            lambda msg: self._show_result(True, str(msg)),
        )

    def _on_save(self) -> None:
        fields = self._collect(require_key=True)
        if fields is None:
            return

        def do():
            # 1) 云端账号配置（网页版共用，Key 由服务端加密）
            api.save_model_config(
                fields["provider"], fields["base_url"], fields["model"], fields["api_key"]
            )
            # 2) **同步一份到本地** —— 本地 AI（图谱抽取 / 记忆提炼 / llm.chat）
            #    直连模型，读的是本地这份。早先只写云端，导致本地那份恒为空、
            #    用户怎么配都提示「未配置模型」（错题本 E-08）。
            #    云端只回掩码、拿不回明文，所以这里必须用输入框里的明文。
            #    本地副本写失败不能让整次保存失败 —— 云端已经成功了。
            local_note = _sync_local(fields)
            cfg = api.get_model_config()
            cfg["_local_note"] = local_note
            return cfg

        self._run("save", do, self._on_saved)

    def _on_saved(self, cfg: Dict[str, Any]) -> None:
        self.key_edit.clear()
        self._config = cfg
        self._render_status(cfg)
        if cfg.get("has_config"):
            self.masked_label.setText(f"当前已存密钥：{cfg.get('api_key_masked') or '—'}")
        note = cfg.get("_local_note") or ""
        msg = "模型配置保存成功，AI 调用已切换为你的模型。"
        # 本地副本失败时只说失败原因，别谎报「已同步」
        msg += note or "本地知识库的图谱抽取与记忆提炼已同步可用。"
        self._show_result(True, msg)
        self.mw.refresh_ai_source()

    def _on_clear(self) -> None:
        # 云端和本地任一有配置，就允许清除（本地可能是单独配的，比如本机 Ollama）
        has_remote = bool(self._config.get("has_config"))
        try:
            has_local = bool(local_db.get_local_model_config().get("has_config"))
        except Exception:  # noqa: BLE001
            has_local = False
        if not has_remote and not has_local:
            self._show_result(False, "当前没有配置自有 Key，无需清除。")
            return
        ans = QMessageBox.question(
            self,
            "清除模型配置",
            "确定清除自有 API Key 吗？\n\n"
            "清除后 AI 调用将回落到平台默认模型；"
            "本地知识库的图谱抽取 / 记忆提炼会随之不可用。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if ans != QMessageBox.StandardButton.Yes:
            return

        def do():
            api.clear_model_config()
            # 本地那份也要清 —— 否则云端清了、本地还留着 Key，
            # 图谱/记忆看起来照常能用，实际用的是一份"已删除"的凭据。
            try:
                local_db.clear_local_model_config()
            except Exception:  # noqa: BLE001
                pass
            return api.get_model_config()

        self._run("clear", do, self._on_cleared)

    def _on_cleared(self, cfg: Dict[str, Any]) -> None:
        self._config = cfg
        self.key_edit.clear()
        self._render_status(cfg)
        self.masked_label.setText("尚未配置自有密钥，当前使用平台默认模型。")
        self._show_result(True, "自有 Key 已清除（含本地知识库副本），已回落到平台默认模型。")
        self.mw.refresh_ai_source()
