"""交流中心页：分享墙（发帖 / 点赞 / 评论）+ 建议反馈（提建议 / 点赞 / 官方回复）。

对齐网页版 `Community.tsx`。两个列表接口在云端都返回**分页对象**
`{items, total, page, page_size}`，已由 api_client._as_page 统一归一，
本页只消费 `{items, total}`。
"""

from typing import Dict, List

from PySide6.QtCore import Qt
from PySide6.QtGui import QDesktopServices, QPixmap
from PySide6.QtCore import QUrl
from PySide6.QtWidgets import (
    QButtonGroup, QComboBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout,
    QHBoxLayout, QLabel, QLineEdit, QMessageBox, QPlainTextEdit, QPushButton,
    QScrollArea, QTabWidget, QVBoxLayout, QWidget,
)

from ... import api_client as api
from ..components import (
    card, empty_state, flow_tags, hint_label, make_title, primary_button,
    section_label,
)

# 分类与状态文案统一从 api_client 取，避免两处维护
POST_CATEGORIES = api.POST_CATEGORIES
FEEDBACK_CATEGORIES = api.FEEDBACK_CATEGORIES
FEEDBACK_STATUS_LABELS = api.FEEDBACK_STATUS_LABELS
FEEDBACK_STATUS_COLORS = {
    "pending": "#d97706",
    "accepted": "#059669",
    "declined": "#64748b",
    "replied": "#1d4ed8",
}


# ==================== 小工具 ====================

def _fmt_time(iso: str) -> str:
    """'2026-09-23 10:20:30.123456' → '09-23 10:20'（跨年的仍显示月日）。"""
    s = (iso or "").strip().replace("T", " ")
    if not s:
        return "-"
    s = s.split(".")[0]
    parts = s.split(" ")
    if len(parts) >= 2:
        day = parts[0]
        day = day[5:] if len(day) >= 10 else day
        return f"{day} {parts[1][:5]}"
    return s


def _excerpt(text: str, limit: int = 140) -> str:
    """压成单行并截断（QLabel 没有 line-clamp，只能自己截）。"""
    s = " ".join((text or "").split())
    if not s:
        return "（无内容）"
    return s if len(s) <= limit else s[:limit] + "…"


def _clear(box: QVBoxLayout) -> None:
    """清空布局里的所有控件（含 spacer）。"""
    while box.count():
        item = box.takeAt(0)
        w = item.widget()
        if w is not None:
            w.setParent(None)
            w.deleteLater()


def _selectable(label: QLabel) -> QLabel:
    label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    return label


def _hex_rgb(color: str) -> tuple:
    c = color.lstrip("#")
    return int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16)


def _chip(text: str, color: str) -> QLabel:
    """带色标签：半透明同色底 + 同色文字 + 同色描边。

    注意不要用 8 位 `#RRGGBBAA` 写法 —— Qt QSS 把 8 位十六进制当作
    `#AARRGGBB` 解析，色相会完全跑偏（ui/components.tag_label 就有这个问题，
    所以本页不直接复用它）。这里用 rgba() 显式表达，保证颜色即所见。
    """
    r, g, b = _hex_rgb(color)
    label = QLabel(text)
    label.setStyleSheet(
        f"background: rgba({r},{g},{b},0.10); color: {color};"
        f"border: 1px solid rgba({r},{g},{b},0.35);"
        "border-radius: 10px; padding: 2px 10px; font-size: 12px;"
    )
    label.setAlignment(Qt.AlignmentFlag.AlignCenter)
    return label


class CategoryBar(QWidget):
    """分类筛选按钮组（互斥），首项固定为「全部」。"""

    def __init__(self, categories: List[str], on_change, parent=None):
        super().__init__(parent)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(8)
        self._group = QButtonGroup(self)
        self._group.setExclusive(True)
        self._values: List[str] = []
        for idx, name in enumerate([""] + list(categories)):
            btn = QPushButton(name or "全部")
            btn.setProperty("class", "chip")
            btn.setCheckable(True)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.setChecked(idx == 0)
            self._group.addButton(btn, idx)
            self._values.append(name)
            lay.addWidget(btn)
        lay.addStretch(1)
        self._group.idClicked.connect(lambda i: on_change(self._values[i]))


def _list_area() -> tuple:
    """返回 (滚动区, 内容布局)；内容布局末尾由调用方补 addStretch(1)。"""
    inner = QWidget()
    box = QVBoxLayout(inner)
    box.setContentsMargins(2, 2, 2, 2)
    box.setSpacing(12)
    scroll = QScrollArea()
    scroll.setWidget(inner)
    scroll.setWidgetResizable(True)
    return scroll, box


# ==================== 发布 / 提交对话框 ====================

class PostDialog(QDialog):
    """发布分享。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("发布分享")
        self.setMinimumWidth(520)
        form = QFormLayout(self)
        self.category = QComboBox()
        self.category.addItems(POST_CATEGORIES)
        self.title = QLineEdit(placeholderText="一句话概括你的分享")
        self.content = QPlainTextEdit()
        self.content.setPlaceholderText("详细说说你的经验或经历…")
        self.content.setFixedHeight(160)
        self.tags = QLineEdit(placeholderText="如：Java, 后端, 校招（逗号分隔，可留空）")
        self.attachments: List[Dict] = []
        self.attach_label = QLabel("未添加附件")
        self.attach_label.setProperty("class", "hint")
        attach_btn = QPushButton("添加附件（图片/Word/PDF）")
        attach_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        attach_btn.clicked.connect(self._add_attachment)
        attach_row = QHBoxLayout()
        attach_row.addWidget(attach_btn)
        attach_row.addWidget(self.attach_label, 1)
        attach_wrap = QWidget()
        attach_wrap.setLayout(attach_row)
        form.addRow("分类", self.category)
        form.addRow("标题*", self.title)
        form.addRow("内容*", self.content)
        form.addRow("标签", self.tags)
        form.addRow("附件", attach_wrap)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("发布")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

    def _add_attachment(self) -> None:
        paths, _ = QFileDialog.getOpenFileNames(
            self, "选择附件", "",
            "图片或文档 (*.jpg *.jpeg *.png *.gif *.webp *.doc *.docx *.pdf)")
        if not paths:
            return
        for p in paths:
            try:
                meta = api.upload_attachment(p)
            except api.ApiError as exc:
                QMessageBox.warning(self, "上传失败", str(exc))
                continue
            if isinstance(meta, dict) and meta.get("url"):
                self.attachments.append(meta)
        self._refresh_attach_label()

    def _refresh_attach_label(self) -> None:
        if not self.attachments:
            self.attach_label.setText("未添加附件")
        else:
            names = [str(a.get("original_name") or a.get("name") or "附件")
                     for a in self.attachments]
            self.attach_label.setText("已添加 " + "、".join(names))

    def _accept(self) -> None:
        if not self.title.text().strip():
            QMessageBox.warning(self, "缺少信息", "请填写标题")
            return
        if not self.content.toPlainText().strip():
            QMessageBox.warning(self, "缺少信息", "请填写内容")
            return
        self.accept()

    def payload(self) -> Dict:
        raw = self.tags.text().replace("，", ",")
        tags = [t.strip() for t in raw.split(",") if t.strip()]
        return {
            "category": self.category.currentText(),
            "title": self.title.text().strip(),
            "content": self.content.toPlainText().strip(),
            "tags": tags,
            "attachments": self.attachments,
        }


class FeedbackDialog(QDialog):
    """提出建议。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("提出建议")
        self.setMinimumWidth(520)
        form = QFormLayout(self)
        self.category = QComboBox()
        self.category.addItems(FEEDBACK_CATEGORIES)
        self.title = QLineEdit(placeholderText="一句话概括你的建议")
        self.content = QPlainTextEdit()
        self.content.setPlaceholderText("描述你希望改进的功能或遇到的问题…")
        self.content.setFixedHeight(160)
        form.addRow("分类", self.category)
        form.addRow("标题*", self.title)
        form.addRow("详细说明*", self.content)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("提交")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

    def _accept(self) -> None:
        if not self.title.text().strip():
            QMessageBox.warning(self, "缺少信息", "请填写标题")
            return
        if not self.content.toPlainText().strip():
            QMessageBox.warning(self, "缺少信息", "请填写详细说明")
            return
        self.accept()

    def payload(self) -> Dict:
        return {
            "category": self.category.currentText(),
            "title": self.title.text().strip(),
            "content": self.content.toPlainText().strip(),
        }


class PostDetailDialog(QDialog):
    """帖子详情：正文 + 评论列表 + 发评论（发完就地刷新）。"""

    def __init__(self, post_id: str, parent=None):
        super().__init__(parent)
        self.post_id = post_id
        self.setWindowTitle("帖子详情")
        self.resize(640, 600)

        root = QVBoxLayout(self)
        root.setContentsMargins(16, 16, 16, 14)
        root.setSpacing(12)

        self.body_host = QWidget()
        self.body_box = QVBoxLayout(self.body_host)
        self.body_box.setContentsMargins(0, 0, 0, 0)
        self.body_box.setSpacing(12)
        scroll = QScrollArea()
        scroll.setWidget(self.body_host)
        scroll.setWidgetResizable(True)
        root.addWidget(scroll, 1)

        row = QHBoxLayout()
        row.setSpacing(8)
        self.comment_input = QLineEdit()
        self.comment_input.setPlaceholderText("写下你的评论…")
        self.comment_input.returnPressed.connect(self._send)
        send = primary_button("发送")
        send.clicked.connect(self._send)
        row.addWidget(self.comment_input, 1)
        row.addWidget(send)
        root.addLayout(row)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.button(QDialogButtonBox.StandardButton.Close).setText("关闭")
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

        self.reload()

    def reload(self) -> None:
        post = api.get_post_detail(self.post_id)
        _clear(self.body_box)
        if not post:
            self.body_box.addWidget(empty_state("帖子已不存在", "可能已被作者或管理员删除"))
            self.body_box.addStretch(1)
            return

        self.setWindowTitle(post.get("title") or "帖子详情")

        # --- 正文卡 ---
        head = card()
        hl = QVBoxLayout(head)
        hl.setContentsMargins(16, 14, 16, 14)
        hl.setSpacing(8)
        title = QLabel(post.get("title") or "（无标题）")
        title.setStyleSheet("font-size: 16px; font-weight: 700;")
        title.setWordWrap(True)
        _selectable(title)
        hl.addWidget(title)

        chips = QHBoxLayout()
        chips.setSpacing(8)
        chips.addWidget(_chip(post.get("category") or "经验分享", "#4f46e5"))
        for t in post.get("tags") or []:
            chips.addWidget(_chip(str(t), "#64748b"))
        chips.addStretch(1)
        hl.addLayout(chips)

        meta = QLabel(
            f"{post.get('author') or '匿名用户'} · {_fmt_time(post.get('created_at'))}"
            f" · 赞 {post.get('like_count') or 0}"
        )
        meta.setProperty("class", "muted")
        hl.addWidget(meta)

        body = QLabel(post.get("content") or "（无内容）")
        body.setWordWrap(True)
        body.setStyleSheet("font-size: 13px;")
        _selectable(body)
        hl.addWidget(body)

        for a in post.get("attachments") or []:
            if a.get("kind") == "image":
                data = api.download_file(a.get("url") or "")
                if data:
                    pix = QPixmap()
                    pix.loadFromData(data)
                    if not pix.isNull():
                        pix = pix.scaledToWidth(320, Qt.TransformationMode.SmoothTransformation)
                        img = QLabel()
                        img.setPixmap(pix)
                        hl.addWidget(img)
            else:
                doc = QLabel("文档：" + str(a.get("original_name") or a.get("name") or "附件"))
                doc.setProperty("class", "hint")
                doc.setWordWrap(True)
                hl.addWidget(doc)
        self.body_box.addWidget(head)

        # --- 评论卡 ---
        comment_card = card()
        cl = QVBoxLayout(comment_card)
        cl.setContentsMargins(16, 14, 16, 14)
        cl.setSpacing(8)
        comments = post.get("comments") or []
        cl.addWidget(section_label(f"评论（{len(comments)}）"))
        if not comments:
            cl.addWidget(hint_label("还没有评论，来抢沙发"))
        for c in comments:
            item = QWidget()
            il = QVBoxLayout(item)
            il.setContentsMargins(0, 0, 0, 0)
            il.setSpacing(4)
            head_row = QHBoxLayout()
            author = QLabel(c.get("author") or "匿名用户")
            author.setStyleSheet("font-size: 12px; font-weight: 600;")
            when = QLabel(_fmt_time(c.get("created_at")))
            when.setProperty("class", "muted")
            head_row.addWidget(author)
            head_row.addStretch(1)
            head_row.addWidget(when)
            il.addLayout(head_row)
            text = QLabel(c.get("content") or "")
            text.setWordWrap(True)
            text.setStyleSheet("font-size: 13px;")
            _selectable(text)
            il.addWidget(text)
            cl.addWidget(item)
        self.body_box.addWidget(comment_card)
        self.body_box.addStretch(1)

    def _send(self) -> None:
        text = self.comment_input.text().strip()
        if not text:
            return
        try:
            api.add_post_comment(self.post_id, text)
        except api.ApiError as exc:
            QMessageBox.warning(self, "评论失败", str(exc))
            return
        self.comment_input.clear()
        self.reload()


# ==================== 主页面 ====================

class CommunityPage(QWidget):
    def __init__(self, main_window):
        super().__init__()
        self.mw = main_window
        self._post_category = ""
        self._fb_category = ""

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.setSpacing(12)
        layout.addWidget(make_title(
            "交流中心",
            "和同行分享求职经验，给产品提建议。",
        ))

        self.tabs = QTabWidget()
        self.tabs.addTab(self._build_posts_tab(), "分享墙")
        self.tabs.addTab(self._build_feedback_tab(), "建议反馈")
        layout.addWidget(self.tabs, 1)

        self.refresh()

    # ---------- 分享墙 ----------

    def _build_posts_tab(self) -> QWidget:
        host = QWidget()
        lay = QVBoxLayout(host)
        lay.setContentsMargins(12, 10, 12, 10)
        lay.setSpacing(12)

        bar = card()
        bl = QHBoxLayout(bar)
        bl.setContentsMargins(16, 14, 16, 14)
        bl.setSpacing(8)
        bl.addWidget(CategoryBar(
            POST_CATEGORIES, self._on_post_category, self))
        refresh_btn = QPushButton("刷新")
        refresh_btn.setProperty("class", "ghost")
        refresh_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        refresh_btn.clicked.connect(self.refresh_posts)
        bl.addWidget(refresh_btn)
        create_btn = primary_button("发布分享")
        create_btn.clicked.connect(self._create_post)
        bl.addWidget(create_btn)
        lay.addWidget(bar)

        self.posts_scroll, self.posts_box = _list_area()
        lay.addWidget(self.posts_scroll, 1)
        return host

    def _on_post_category(self, category: str) -> None:
        self._post_category = category
        self.refresh_posts()

    def refresh_posts(self) -> None:
        data = api.list_posts(self._post_category, page_size=50)
        posts = data["items"]
        _clear(self.posts_box)
        if not posts:
            hint = "该分类下还没有分享" if self._post_category else "还没有分享"
            self.posts_box.addWidget(empty_state(hint, "点右上角「发布分享」写下你的第一条经验"))
            self.posts_box.addStretch(1)
            return
        for p in posts:
            self.posts_box.addWidget(self._post_card(p))
        self.posts_box.addStretch(1)

    def _attachment_line(self, attachments: List[Dict]) -> QWidget:
        """附件摘要行：图片 N 张 / 文档 N 个。"""
        imgs = sum(1 for a in attachments if a.get("kind") == "image")
        docs = sum(1 for a in attachments if a.get("kind") == "doc")
        parts = []
        if imgs:
            parts.append(f"图片 {imgs}")
        if docs:
            parts.append(f"文档 {docs}")
        label = QLabel("附件：" + "、".join(parts))
        label.setProperty("class", "hint")
        return label

    def _post_card(self, p: Dict) -> QWidget:
        frame = card()
        lay = QVBoxLayout(frame)
        lay.setContentsMargins(16, 14, 16, 12)
        lay.setSpacing(8)

        top = QHBoxLayout()
        top.setSpacing(12)
        title = QLabel(p.get("title") or "（无标题）")
        title.setStyleSheet("font-size: 15px; font-weight: 700;")
        title.setWordWrap(True)
        _selectable(title)
        top.addWidget(title, 1)
        top.addWidget(_chip(p.get("category") or "经验分享", "#4f46e5"),
                       0, Qt.AlignmentFlag.AlignTop)
        lay.addLayout(top)

        body = QLabel(_excerpt(p.get("content"), 140))
        body.setProperty("class", "hint")
        body.setWordWrap(True)
        lay.addWidget(body)

        if p.get("tags"):
            lay.addWidget(flow_tags(p["tags"], "#64748b", max_count=8))

        if p.get("attachments"):
            lay.addWidget(self._attachment_line(p["attachments"]))

        bottom = QHBoxLayout()
        bottom.setSpacing(8)
        meta = QLabel(f"{p.get('author') or '匿名用户'} · {_fmt_time(p.get('created_at'))}")
        meta.setProperty("class", "muted")
        bottom.addWidget(meta)
        bottom.addStretch(1)

        like = QPushButton(f"赞 {p.get('like_count') or 0}")
        like.setProperty("class", "chip")
        like.setCursor(Qt.CursorShape.PointingHandCursor)
        like.setToolTip("点赞 / 取消点赞")
        like.clicked.connect(lambda _=False, pid=p["id"]: self._like_post(pid))
        bottom.addWidget(like)

        comments = QLabel(f"评论 {p.get('comment_count') or 0}")
        comments.setProperty("class", "muted")
        bottom.addWidget(comments)

        view = QPushButton("查看详情")
        view.setProperty("class", "ghost")
        view.setCursor(Qt.CursorShape.PointingHandCursor)
        view.clicked.connect(lambda _=False, pid=p["id"]: self._open_post(pid))
        bottom.addWidget(view)
        lay.addLayout(bottom)
        return frame

    def _like_post(self, post_id: str) -> None:
        try:
            api.toggle_post_like(post_id)
        except api.ApiError as exc:
            QMessageBox.warning(self, "操作失败", str(exc))
            return
        self.refresh_posts()

    def _open_post(self, post_id: str) -> None:
        PostDetailDialog(post_id, self).exec()
        # 详情里可能发了评论/点了赞 → 回来后刷新计数
        self.refresh_posts()

    def _create_post(self) -> None:
        dlg = PostDialog(self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        payload = dlg.payload()
        try:
            api.create_post(payload["title"], payload["content"],
                            payload["category"], payload["tags"],
                            payload.get("attachments"))
        except api.ApiError as exc:
            QMessageBox.warning(self, "发布失败", str(exc))
            return
        self.refresh_posts()

    # ---------- 建议反馈 ----------

    def _build_feedback_tab(self) -> QWidget:
        host = QWidget()
        lay = QVBoxLayout(host)
        lay.setContentsMargins(12, 10, 12, 10)
        lay.setSpacing(12)

        bar = card()
        bl = QHBoxLayout(bar)
        bl.setContentsMargins(16, 14, 16, 14)
        bl.setSpacing(8)
        bl.addWidget(CategoryBar(
            FEEDBACK_CATEGORIES, self._on_fb_category, self))
        refresh_btn = QPushButton("刷新")
        refresh_btn.setProperty("class", "ghost")
        refresh_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        refresh_btn.clicked.connect(self.refresh_feedbacks)
        bl.addWidget(refresh_btn)
        create_btn = primary_button("提建议")
        create_btn.clicked.connect(self._create_feedback)
        bl.addWidget(create_btn)
        lay.addWidget(bar)

        self.fb_scroll, self.fb_box = _list_area()
        lay.addWidget(self.fb_scroll, 1)
        return host

    def _on_fb_category(self, category: str) -> None:
        self._fb_category = category
        self.refresh_feedbacks()

    def refresh_feedbacks(self) -> None:
        data = api.list_feedbacks(self._fb_category, page_size=50)
        items = data["items"]
        _clear(self.fb_box)
        if not items:
            hint = "该分类下还没有建议" if self._fb_category else "还没有建议"
            self.fb_box.addWidget(empty_state(hint, "你的建议能帮助产品变得更好"))
            self.fb_box.addStretch(1)
            return
        for f in items:
            self.fb_box.addWidget(self._feedback_card(f))
        self.fb_box.addStretch(1)

    def _feedback_card(self, f: Dict) -> QWidget:
        frame = card()
        lay = QVBoxLayout(frame)
        lay.setContentsMargins(16, 14, 16, 12)
        lay.setSpacing(8)

        top = QHBoxLayout()
        top.setSpacing(8)
        status = f.get("status") or "pending"
        top.addWidget(_chip(f.get("category") or "功能建议", "#4f46e5"))
        top.addWidget(_chip(
            FEEDBACK_STATUS_LABELS.get(status, status),
            FEEDBACK_STATUS_COLORS.get(status, "#64748b")))
        top.addStretch(1)
        like = QPushButton(f"赞 {f.get('like_count') or 0}")
        like.setProperty("class", "chip")
        like.setCursor(Qt.CursorShape.PointingHandCursor)
        like.setToolTip("赞同 / 取消赞同")
        like.clicked.connect(lambda _=False, fid=f["id"]: self._like_feedback(fid))
        top.addWidget(like)
        lay.addLayout(top)

        title = QLabel(f.get("title") or "（无标题）")
        title.setStyleSheet("font-size: 15px; font-weight: 700;")
        title.setWordWrap(True)
        _selectable(title)
        lay.addWidget(title)

        body = QLabel(f.get("content") or "（无内容）")
        body.setWordWrap(True)
        body.setStyleSheet("font-size: 13px;")
        _selectable(body)
        lay.addWidget(body)

        if f.get("admin_reply"):
            reply = card(highlight=True)
            rl = QVBoxLayout(reply)
            rl.setContentsMargins(12, 10, 12, 10)
            rl.setSpacing(4)
            rl.addWidget(section_label("官方回复"))
            text = QLabel(f["admin_reply"])
            text.setWordWrap(True)
            text.setStyleSheet("font-size: 13px;")
            _selectable(text)
            rl.addWidget(text)
            lay.addWidget(reply)

        meta = QLabel(f"{f.get('author') or '匿名用户'} · {_fmt_time(f.get('created_at'))}")
        meta.setProperty("class", "muted")
        lay.addWidget(meta)
        return frame

    def _like_feedback(self, feedback_id: str) -> None:
        try:
            api.toggle_feedback_like(feedback_id)
        except api.ApiError as exc:
            QMessageBox.warning(self, "操作失败", str(exc))
            return
        self.refresh_feedbacks()

    def _create_feedback(self) -> None:
        dlg = FeedbackDialog(self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        payload = dlg.payload()
        try:
            api.create_feedback(payload["category"], payload["title"], payload["content"])
        except api.ApiError as exc:
            QMessageBox.warning(self, "提交失败", str(exc))
            return
        QMessageBox.information(self, "已提交", "感谢反馈！管理员处理后会在此显示回复。")
        self.refresh_feedbacks()

    # ---------- 对外 ----------

    def refresh(self) -> None:
        """供主窗口切换页面 / 登录后刷新调用。"""
        self.refresh_posts()
        self.refresh_feedbacks()
