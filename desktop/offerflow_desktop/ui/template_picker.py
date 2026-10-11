"""版式模板选择器：直接渲染真实 PDF 做预览，所见即所得。

不用"画个示意图"的方式来预览版式——那样和导出结果总会有出入。
这里把模板真实排版成 PDF，再用 Qt 自带的 QtPdf 渲染成图，
所以用户看到的就是最终导出文件的样子。

**版式 / 外观 / 顺序 是三个独立的旋钮**（2026-10-07 拆开）：

- 版式：选哪套模板（标题装饰、对齐、疏密结构）
- 外观：强调色 + 字号缩放 —— 换版式时**原样保留**，不会被覆盖
- 顺序：正文分区谁先谁后

三者任一变化都会重渲染预览；缓存的 key 里带着这三样，
所以不会出现「改了颜色却还显示旧图」这种最难查的假一致。
"""

from __future__ import annotations

import os
import tempfile
from typing import Any, Dict, List, Optional, Tuple

from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QSize, Qt, QTimer
from PySide6.QtGui import QColor, QImage, QPixmap
from PySide6.QtPdf import QPdfDocument
from PySide6.QtWidgets import (
    QAbstractItemView, QColorDialog, QDialog, QFrame, QHBoxLayout, QLabel,
    QListWidget, QListWidgetItem, QPlainTextEdit, QPushButton, QScrollArea,
    QSlider, QTabWidget, QVBoxLayout, QWidget,
)

from .. import database as local_db
from ..core.sections import BODY_SECTIONS, SECTION_LABELS, normalize_section_order
from ..services import pdf_exporter, templates
from .components import hint_label, primary_button


def load_export_options() -> Tuple[templates.ResumeAppearance, List[str]]:
    """读上次用的外观与分区顺序（导出对话框与导出调用共用同一份）。

    读失败一律退回默认值：配置文件是本机 JSON，被手工改坏或跨版本残留
    都不该让「导出」这个按钮点不动。
    """
    try:
        cfg = local_db.get_system_config()
    except Exception:
        cfg = {}
    try:
        appearance = templates.ResumeAppearance.from_dict(cfg.get("resume_appearance"))
    except Exception:
        appearance = templates.ResumeAppearance()
    try:
        order = normalize_section_order(cfg.get("resume_section_order"))
    except Exception:
        order = normalize_section_order(None)
    return appearance, order


def save_export_options(appearance: templates.ResumeAppearance,
                        order: Optional[Any]) -> None:
    """把外观与顺序写回本机配置（用导出时的选择记住，下次打开就是它）。"""
    try:
        local_db.set_system_config(
            resume_appearance=appearance.to_dict(),
            resume_section_order=normalize_section_order(order),
        )
    except Exception:
        pass                                   # 记不住偏好不该打断导出


def _extract_pdf_text(payload: bytes) -> Tuple[str, str]:
    """把 PDF 字节写成临时文件回读成文本，返回 ``(文本, 一行说明)``。

    必须落一次临时文件：本机的解析引擎都吃路径而不是字节流。
    失败一律返回空文本 + 原因，**不抛异常** —— 这是预览面板的附加信息，
    读不出来不该让整个对话框打不开。
    """
    if not payload:
        return "", "没有可回读的内容"
    try:
        from ..local_ai import parse as local_parse
    except Exception as exc:                              # noqa: BLE001
        return "", f"本地解析模块不可用：{exc}"
    path = os.path.join(tempfile.gettempdir(), "_offerflow_ats_view.pdf")
    try:
        with open(path, "wb") as fh:
            fh.write(payload)
        result = local_parse.parse_pdf(path)
    except Exception as exc:                              # noqa: BLE001
        return "", f"回读失败：{exc}"
    text = (getattr(result, "text", "") or "").strip()
    if not text:
        return "", ("回读不到文字：PDF 里的字形可能被转成了图形，"
                    "招聘系统同样读不到")
    note = (f"回读 {len(text)} 字 · 引擎 {getattr(result, 'engine', '') or '未知'}"
            f" · {getattr(result, 'pages', 0) or 0} 页")
    return text, note


class TemplatePickerDialog(QDialog):
    """列出全部版式模板，选中即在右侧渲染真实 PDF 预览。"""

    RESULT_DOCX = 2
    RESULT_PDF = 3

    def __init__(self, structured: Dict[str, Any], parent=None,
                 default_key: Optional[str] = None, from_blocks: bool = False,
                 default_appearance: Optional[templates.ResumeAppearance] = None,
                 default_order: Optional[Any] = None):
        super().__init__(parent)
        self.setWindowTitle("选择版式")
        self.resize(1080, 760)
        self.structured = structured or {}
        self.from_blocks = from_blocks
        self.selected_key = templates.get_template(default_key).key
        self.selected_appearance = (default_appearance
                                    or templates.ResumeAppearance()).normalized()
        self.selected_order: List[str] = normalize_section_order(default_order)
        self._filter = "all"          # all / single / ats（列表筛选）
        # 缓存里连 PDF 字节一起留着：ATS 视角要回读它，重渲染一次既慢又可能
        # 和预览图不是同一份（那就成了另一种假一致）。
        self._cache: Dict[Tuple[Any, ...],
                          Tuple[Optional[QImage], str, bytes]] = {}
        self._doc = QPdfDocument(self)
        self._buffer: Optional[QBuffer] = None
        self._tmp_path = ""
        # 字号滑块的防抖：拖动过程中每格都重排一次 PDF 会卡，
        # 停下来 180ms 再渲染，手感与开销都合适。
        self._scale_timer = QTimer(self)
        self._scale_timer.setSingleShot(True)
        self._scale_timer.setInterval(180)
        self._scale_timer.timeout.connect(self._on_scale_settled)
        self._build()
        self._select(self.selected_key)

    # ---------- 界面 ----------

    def _build(self) -> None:
        layout = QHBoxLayout(self)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(16)

        left = QVBoxLayout()
        left.setSpacing(8)
        left.addWidget(QLabel("版式模板"))
        left.addLayout(self._filter_row())
        self.list = QListWidget()
        self.list.setFixedWidth(250)
        self.list.currentRowChanged.connect(self._on_row_changed)
        self._refresh_template_list()
        left.addWidget(self.list, 3)

        left.addWidget(self._divider())
        left.addWidget(QLabel("外观（换版式不会丢）"))
        left.addLayout(self._appearance_row())
        self.capability_label = hint_label("")
        self.capability_label.setWordWrap(True)
        left.addWidget(self.capability_label)

        left.addWidget(self._divider())
        left.addWidget(QLabel("正文分区顺序（可拖动排序）"))
        self.order_list = QListWidget()
        self.order_list.setFixedWidth(250)
        self.order_list.setFixedHeight(150)
        # 拖放排序：上移/下移按钮适合微调，卡片多的时候拖动更快。
        # 用 InternalMove 而不是自由拖动：只允许在本列表内部换位置，
        # 拖到列表外面不会产生"复制/移动"语义上的意外。
        self.order_list.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self.order_list.setDefaultDropAction(Qt.DropAction.MoveAction)
        self.order_list.setDragEnabled(True)
        self.order_list.setAcceptDrops(True)
        self.order_list.setDropIndicatorShown(True)
        self.order_list.model().rowsMoved.connect(self._on_order_moved)
        left.addWidget(self.order_list, 2)
        left.addLayout(self._order_buttons())
        left.addStretch(1)
        layout.addLayout(left)

        right = QVBoxLayout()
        right.setSpacing(8)
        head = QHBoxLayout()
        self.title_label = QLabel("")
        self.title_label.setStyleSheet("font-size:14px; font-weight:600;")
        head.addWidget(self.title_label)
        head.addStretch(1)
        self.info_label = hint_label("")
        head.addWidget(self.info_label)
        right.addLayout(head)

        self.preview_tabs = QTabWidget()

        # ① 版式预览：真实渲染的 PDF 图（所见即所得）
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.canvas = QLabel("正在生成预览…")
        self.canvas.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignHCenter)
        self.scroll.setWidget(self.canvas)
        self.preview_tabs.addTab(self.scroll, "版式预览")

        # ② ATS 视角：**回读刚生成的那份 PDF**，显示招聘系统实际读到的文本。
        #    不从结构化数据拼："那份 PDF 里到底有没有这几个字"才是问题所在，
        #    拼出来的文本永远是对的，看不出字体子集化/挤压截断造成的丢失。
        ats_host = QWidget()
        av = QVBoxLayout(ats_host)
        av.setContentsMargins(10, 8, 10, 8)
        av.setSpacing(6)
        self.ats_note_label = hint_label("")
        self.ats_note_label.setWordWrap(True)
        av.addWidget(self.ats_note_label)
        self.ats_view = QPlainTextEdit()
        self.ats_view.setReadOnly(True)
        self.ats_view.setPlaceholderText(
            "切到这一页会把刚生成的 PDF 回读一遍，显示招聘系统实际读到的文字。")
        av.addWidget(self.ats_view, 1)
        self.preview_tabs.addTab(ats_host, "ATS 视角")
        self.preview_tabs.currentChanged.connect(self._on_preview_tab_changed)
        right.addWidget(self.preview_tabs, 1)

        btn_row = QHBoxLayout()
        btn_row.addStretch(1)
        cancel = QPushButton("取消")
        cancel.clicked.connect(self.reject)
        btn_row.addWidget(cancel)
        self.docx_btn = primary_button("导出 Word")
        self.docx_btn.clicked.connect(lambda: self.done(self.RESULT_DOCX))
        btn_row.addWidget(self.docx_btn)
        self.pdf_btn = primary_button("导出 PDF")
        self.pdf_btn.clicked.connect(lambda: self.done(self.RESULT_PDF))
        btn_row.addWidget(self.pdf_btn)
        right.addLayout(btn_row)

        layout.addLayout(right, 1)
        self._refresh_order_list()
        self._refresh_capability()

    # ---------- 列表筛选（全部 / 单栏 / ATS 安全） ----------

    FILTERS = (("all", "全部"), ("single", "单栏"), ("ats", "ATS 安全"))

    def _filter_row(self) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setSpacing(4)
        self.filter_buttons: Dict[str, QPushButton] = {}
        for key, label in self.FILTERS:
            btn = QPushButton(label)
            btn.setCheckable(True)
            btn.setFixedWidth(72)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.clicked.connect(lambda _c=False, k=key: self._on_filter_changed(k))
            self.filter_buttons[key] = btn
            row.addWidget(btn)
        row.addStretch(1)
        self._sync_filter_buttons()
        return row

    def _sync_filter_buttons(self) -> None:
        for key, btn in self.filter_buttons.items():
            btn.setChecked(key == self._filter)

    def _on_filter_changed(self, key: str) -> None:
        if key not in self.filter_buttons or key == self._filter:
            self._sync_filter_buttons()      # 再点当前筛选项：只保证状态不漂
            return
        self._filter = key
        self._sync_filter_buttons()
        self._refresh_template_list()
        self._select(self.selected_key)

    def _template_passes(self, tpl) -> bool:
        """筛选判据用**出厂模板**，不叠加外观。

        若用"叠加外观后的有效模板"，把字号拖到 85% 会让「ATS 安全」的筛选
        结果直接空掉 —— 列表看起来像坏了。外观带来的风险改由左侧能力标注
        如实说明（那里是现算的），两处各管一件事。
        """
        if self._filter == "single":
            return tpl.columns == 1
        if self._filter == "ats":
            return tpl.ats_safe
        return True

    def _refresh_template_list(self) -> None:
        """按当前筛选重建列表；被筛掉的当前选中项不会静默改选。"""
        keep = self.selected_key
        self.list.blockSignals(True)
        self.list.clear()
        for tpl in templates.TEMPLATES:
            if not self._template_passes(tpl):
                continue
            item = QListWidgetItem(f"{tpl.label}\n{tpl.desc}")
            item.setData(Qt.ItemDataRole.UserRole, tpl.key)
            item.setToolTip(tpl.desc)
            self.list.addItem(item)
        if self.list.count() == 0:
            # 占位项不带 key，_on_row_changed 会忽略它（不会误当成选中）
            self.list.addItem(QListWidgetItem("（没有符合该筛选条件的版式）"))
        self.list.blockSignals(False)
        for i in range(self.list.count()):
            if self.list.item(i).data(Qt.ItemDataRole.UserRole) == keep:
                self.list.setCurrentRow(i)
                return
        # 当前版式被筛掉了：不清掉 selected_key（页面仍按它导出），
        # 只把高亮撤掉 —— 让高亮停在别的条目上才是真的误导。
        self.list.setCurrentRow(-1)

    # ---------- ATS 视角 ----------

    def _on_preview_tab_changed(self, index: int) -> None:
        if index == 1:
            self._refresh_ats_text()

    def _refresh_ats_text(self) -> None:
        """回读刚生成的 PDF，显示招聘系统实际读到的文字。"""
        cached = self._cache.get(self._cache_key())
        payload = cached[2] if cached else b""
        tpl = templates.apply_appearance(
            templates.get_template(self.selected_key), self.selected_appearance)
        notes = "；".join(tpl.ats_notes) or "无已知 ATS 可读性风险"
        text, note = _extract_pdf_text(payload)
        self.ats_note_label.setText(f"{tpl.layout_label} · {notes}\n{note}")
        self.ats_view.setPlainText(text)

    def _divider(self) -> QFrame:
        line = QFrame()
        line.setFrameShape(QFrame.Shape.HLine)
        line.setFrameShadow(QFrame.Shadow.Plain)
        return line

    def _appearance_row(self) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setSpacing(6)
        self.accent_btn = QPushButton("强调色")
        self.accent_btn.setFixedWidth(84)
        self.accent_btn.clicked.connect(self._pick_accent)
        row.addWidget(self.accent_btn)

        row.addWidget(QLabel("字号"))
        self.scale_slider = QSlider(Qt.Orientation.Horizontal)
        self.scale_slider.setMinimum(int(templates.ResumeAppearance.MIN_SCALE * 100))
        self.scale_slider.setMaximum(int(templates.ResumeAppearance.MAX_SCALE * 100))
        self.scale_slider.setSingleStep(5)
        self.scale_slider.setValue(int(round(self.selected_appearance.font_scale * 100)))
        self.scale_slider.valueChanged.connect(self._on_scale_changed)
        self.scale_slider.sliderReleased.connect(self._on_scale_settled)
        row.addWidget(self.scale_slider, 1)

        self.scale_label = QLabel("")
        self.scale_label.setFixedWidth(40)
        row.addWidget(self.scale_label)

        self.reset_look_btn = QPushButton("还原")
        self.reset_look_btn.setFixedWidth(56)
        self.reset_look_btn.setToolTip("把颜色与字号恢复成版式自带的")
        self.reset_look_btn.clicked.connect(self._reset_appearance)
        row.addWidget(self.reset_look_btn)
        self._sync_appearance_widgets()
        return row

    def _order_buttons(self) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setSpacing(6)
        self.up_btn = QPushButton("上移")
        self.up_btn.clicked.connect(lambda: self._move_order(-1))
        row.addWidget(self.up_btn)
        self.down_btn = QPushButton("下移")
        self.down_btn.clicked.connect(lambda: self._move_order(1))
        row.addWidget(self.down_btn)
        self.reset_order_btn = QPushButton("默认顺序")
        self.reset_order_btn.clicked.connect(self._reset_order)
        row.addWidget(self.reset_order_btn)
        row.addStretch(1)
        return row

    # ---------- 外观 ----------

    def _sync_appearance_widgets(self) -> None:
        look = self.selected_appearance
        color = look.accent_hex or templates.get_template(self.selected_key).accent_hex
        swatch = QColor("#" + color.lstrip("#"))
        self.accent_btn.setStyleSheet(
            "text-align:center; border:1px solid #999; background:%s; color:%s;"
            % (swatch.name(), "#FFFFFF" if swatch.lightness() < 140 else "#111111"))
        self.scale_slider.setValue(int(round(look.font_scale * 100)))
        self.scale_label.setText("%d%%" % int(round(look.font_scale * 100)))

    def _pick_accent(self) -> None:
        current = self.selected_appearance.accent_hex \
            or templates.get_template(self.selected_key).accent_hex
        picked = QColorDialog.getColor(QColor("#" + current.lstrip("#")), self,
                                       "选择强调色")
        if not picked.isValid():
            return
        self.selected_appearance = templates.ResumeAppearance(
            accent_hex=picked.name().lstrip("#"),
            font_scale=self.selected_appearance.font_scale,
            font_cn=self.selected_appearance.font_cn,
            font_en=self.selected_appearance.font_en,
        ).normalized()
        self._sync_appearance_widgets()
        self._refresh_capability()
        self._select(self.selected_key, force=True)

    def _on_scale_changed(self, value: int) -> None:
        self.scale_label.setText("%d%%" % value)
        self._scale_timer.start()

    def _on_scale_settled(self) -> None:
        value = int(self.scale_slider.value())
        look = templates.ResumeAppearance(
            accent_hex=self.selected_appearance.accent_hex,
            font_scale=value / 100.0,
            font_cn=self.selected_appearance.font_cn,
            font_en=self.selected_appearance.font_en,
        ).normalized()
        if look.font_scale == self.selected_appearance.font_scale:
            return
        self.selected_appearance = look
        self.scale_label.setText("%d%%" % int(round(look.font_scale * 100)))
        self._refresh_capability()
        self._select(self.selected_key, force=True)

    def _reset_appearance(self) -> None:
        self.selected_appearance = templates.ResumeAppearance()
        self._sync_appearance_widgets()
        self._refresh_capability()
        self._select(self.selected_key, force=True)

    # ---------- 分区顺序 ----------

    def _refresh_order_list(self) -> None:
        self.order_list.blockSignals(True)
        self.order_list.clear()
        for name in self.selected_order:
            item = QListWidgetItem(SECTION_LABELS.get(name, name))
            item.setData(Qt.ItemDataRole.UserRole, name)
            self.order_list.addItem(item)
        self.order_list.blockSignals(False)

    def _move_order(self, delta: int) -> None:
        row = self.order_list.currentRow()
        if row < 0:
            return
        target = row + delta
        if not (0 <= target < len(self.selected_order)):
            return
        order = list(self.selected_order)
        order[row], order[target] = order[target], order[row]
        self.selected_order = normalize_section_order(order)
        self._refresh_order_list()
        self.order_list.setCurrentRow(target)
        self._select(self.selected_key, force=True)

    def _reset_order(self) -> None:
        self.selected_order = normalize_section_order(BODY_SECTIONS)
        self._refresh_order_list()
        self._select(self.selected_key, force=True)

    def _on_order_moved(self, *_args) -> None:
        """拖放结束后把列表里的顺序收回来。

        延到事件循环下一拍再读：``rowsMoved`` 是在模型搬运过程中发出的，
        此刻立刻重建列表有可能打断它自己的操作。
        """
        QTimer.singleShot(0, self._collect_order_after_drag)

    def _collect_order_after_drag(self) -> None:
        order: List[str] = []
        for i in range(self.order_list.count()):
            name = self.order_list.item(i).data(Qt.ItemDataRole.UserRole)
            if name:
                order.append(name)
        if not order or order == list(self.selected_order):
            return                          # 拖回原位不算改动，别白重渲染一次
        self.selected_order = normalize_section_order(order)
        self._refresh_order_list()
        self._select(self.selected_key, force=True)

    # ---------- 能力标注 ----------

    def _refresh_capability(self) -> None:
        """把当前版式（含外观）的 ATS 影响如实显示出来。

        用的是**有效模板**而不是出厂模板：字号被拖到 85% 之后，
        这里必须跟着出现「低于 9pt」的提醒，否则就是自相矛盾的界面。
        """
        tpl = templates.apply_appearance(
            templates.get_template(self.selected_key), self.selected_appearance)
        parts = [tpl.layout_label]
        notes = tpl.ats_notes
        if notes:
            parts.append("；".join(notes))
            self.capability_label.setText("⚠ " + "；".join(parts))
            self.capability_label.setStyleSheet("color:#B45309;")
        else:
            parts.append("无已知 ATS 可读性风险")
            self.capability_label.setText("· " + "；".join(parts))
            self.capability_label.setStyleSheet("")

    # ---------- 交互 ----------

    def _on_row_changed(self, row: int) -> None:
        if row < 0:
            return
        key = self.list.item(row).data(Qt.ItemDataRole.UserRole)
        if key and key != self.selected_key:
            self.selected_key = templates.get_template(key).key
            # 换版式时**不动**外观（这正是把两条轴拆开的意义），
            # 但要重新算能力标注：同样的外观叠在不同版式上风险不同。
            self._sync_appearance_widgets()
            self._refresh_capability()
            self._select(self.selected_key)

    def _select(self, key: str, force: bool = False) -> None:
        self.selected_key = templates.get_template(key).key
        tpl = templates.get_template(self.selected_key)
        self.title_label.setText(f"{tpl.label} · {tpl.desc}")
        for i in range(self.list.count()):
            if self.list.item(i).data(Qt.ItemDataRole.UserRole) == self.selected_key:
                if self.list.currentRow() != i:
                    self.list.blockSignals(True)
                    self.list.setCurrentRow(i)
                    self.list.blockSignals(False)
                break

        cache_key = self._cache_key()
        cached = self._cache.get(cache_key)
        if cached is None or force:
            cached = self._render_preview(self.selected_key)
            self._cache[cache_key] = cached
        image, info, _payload = cached
        if image is not None:
            self.canvas.setPixmap(QPixmap.fromImage(image))
        else:
            self.canvas.setText(info)
        self.info_label.setText(info)
        # 换了版式/外观/顺序，ATS 视角里那份文本就过期了
        self.ats_view.clear()
        if self.preview_tabs.currentIndex() == 1:
            self._refresh_ats_text()

    def _cache_key(self) -> Tuple[Any, ...]:
        """预览缓存 key：三项设置全在里面。

        漏掉任何一项都会出现「改了颜色/顺序却还显示旧预览」——
        这种假一致比报错还难发现，所以宁可 key 粗一点也不省这一步。
        """
        look = self.selected_appearance
        return (self.selected_key, look.accent_hex, round(look.font_scale, 3),
                tuple(self.selected_order))

    def _render_preview(self, key: str) -> Tuple[Optional[QImage], str, bytes]:
        """渲染一次预览，返回 (图, 一行说明, PDF 字节)。

        字节要一起返回：ATS 视角需要的是**这一份**PDF 回读出来的文本，
        另渲染一次就可能拿到另一份（缓存的是同一份才谈得上一致）。
        """
        try:
            if self.from_blocks:
                payload, layout = pdf_exporter.render_pdf_blocks(
                    self.structured, template_key=key,
                    order=self.selected_order,
                    appearance=self.selected_appearance)
            else:
                payload, layout = pdf_exporter.render_pdf_bytes(
                    self.structured, template_key=key,
                    order=self.selected_order,
                    appearance=self.selected_appearance)
        except Exception as exc:                      # 预览失败不该打断用户
            return None, f"预览生成失败：{exc}", b""
        image = self._to_image(payload)
        if image is None:
            return None, "预览渲染失败", payload

        parts = []
        fill = layout.get("fill_ratio")
        if fill is not None:
            parts.append(f"占一页约 {fill:.0%}")
        if layout.get("overflow"):
            parts.append("内容超出页面，建议精简")
        scale = layout.get("scale") or 1.0
        if abs(scale - 1.0) > 0.02:
            parts.append(f"自动缩放 ×{scale:.2f}")
        return image, " · ".join(parts) if parts else "版式就绪", payload

    def _to_image(self, payload: bytes) -> Optional[QImage]:
        """优先走内存缓冲；个别环境不支持时退回临时文件。"""
        loaded = False
        try:
            self._buffer = QBuffer()
            self._buffer.setData(QByteArray(payload))
            self._buffer.open(QIODevice.OpenModeFlag.ReadOnly)
            self._doc.close()
            self._doc.load(self._buffer)
            loaded = self._doc.pageCount() > 0
        except Exception:
            loaded = False
        if not loaded:
            try:
                path = os.path.join(tempfile.gettempdir(), "_offerflow_preview.pdf")
                with open(path, "wb") as fh:
                    fh.write(payload)
                self._tmp_path = path
                self._doc.close()
                self._doc.load(path)
                loaded = self._doc.pageCount() > 0
            except Exception:
                return None
        if not loaded:
            return None
        size = self._doc.pagePointSize(0)
        scale = 1.15
        return self._doc.render(
            0, QSize(int(size.width() * scale), int(size.height() * scale)))
