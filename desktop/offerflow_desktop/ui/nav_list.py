# -*- coding: utf-8 -*-
"""侧栏导航控件 —— 可滚动的「两行」导航列表。

## 为什么不用 QListWidget

原来用 ``QListWidget`` + ``setItemWidget`` 做不到这套设计，原因是：

1. **两行条目**：``QListWidgetItem`` 只能给一个文本；副标题要靠自绘 delegate，
   而 delegate 又拿不到 QSS（颜色要手写两套主题，正是本次想消除的样式漂移）。
2. **选中态要靠 QSS 属性选择器**：``::item:selected`` 只能改背景/文字色，
   做不出「左侧 3px 竖条 + 图标换色 + 副标题变深」这套组合。
3. **分组标题**原先靠 ``QListWidgetItem`` + ``NoItemFlags`` 伪装，于是它
   **也是一个 row** —— 启动时 ``setCurrentRow(0)`` 正好落在分组标题上，
   选中态被吞掉，界面起来了却没有任何高亮（实测 16/16 项 ``isSelected=False``）。

现在换成「QScrollArea + 真实 QWidget 行」，布局、状态、主题全部走 QSS 与
动态属性，行为可用几何断言直接验证。
"""

from __future__ import annotations

from typing import Dict, Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import (
    QFrame, QHBoxLayout, QLabel, QPushButton, QScrollArea, QSizePolicy,
    QVBoxLayout, QWidget,
)

from . import tokens
from .components import set_prop
from .icons import nav_icon
from .nav_registry import NavGroup, NavItem


class NavRow(QFrame):
    """一行导航条目：左侧竖条 + 图标 + (主标题 / 副标题)。

    点击 → ``activated(key)``。用 ``mousePressEvent`` 而不是 ``clicked`` 信号，
    因为 ``QFrame`` 没有 clicked；同时给键盘留了 Enter / Space。
    """

    activated = Signal(str)

    def __init__(self, item: NavItem, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setObjectName("NavRow")
        self.item = item
        self.key = item.key
        self.setFixedHeight(tokens.NAV_ROW_H)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setProperty("active", "false")

        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 10, 0)
        lay.setSpacing(0)

        # 选中态左侧竖条：只在激活时着色，平时透明（占位以保证文字不跳动）
        self.indicator = QFrame(self)
        self.indicator.setObjectName("NavIndicator")
        self.indicator.setFixedSize(tokens.NAV_INDICATOR_W, 18)
        self.indicator.setProperty("active", "false")
        lay.addWidget(self.indicator, 0, Qt.AlignmentFlag.AlignVCenter)
        lay.addSpacing(7)

        self.icon_label = QLabel(self)
        self.icon_label.setObjectName("NavIcon")
        self.icon_label.setFixedSize(tokens.NAV_ICON, tokens.NAV_ICON)
        self.icon_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(self.icon_label, 0, Qt.AlignmentFlag.AlignVCenter)
        lay.addSpacing(10)

        text_col = QVBoxLayout()
        text_col.setContentsMargins(0, 0, 0, 0)
        text_col.setSpacing(0)
        self.title = QLabel(item.label, self)
        self.title.setObjectName("NavRowTitle")
        self.title.setProperty("active", "false")
        self.sub = QLabel(item.subtitle, self)
        self.sub.setObjectName("NavRowSub")
        self.sub.setProperty("active", "false")
        text_col.addWidget(self.title)
        text_col.addWidget(self.sub)
        lay.addLayout(text_col, 1)

        self._dark: Optional[bool] = None
        self.set_dark(tokens.is_dark())

    # ------------------------------------------------------------------
    def _refresh_icon(self) -> None:
        """按「主题 + 是否选中」重画图标。

        为什么要这一步：``nav_icon()`` 返回的 QIcon 里有 Normal / Selected 两套配色，
        但 Selected 模式**只有放在 QListWidget / ItemView 里才会被自动选中**。
        现在图标挂在普通 QLabel 上，Qt 永远不会自己切到 Selected ——
        实测就是「标题变靛蓝了、图标还是灰的」。这里显式按状态取 pixmap。
        """
        icon = nav_icon(self.item.icon, size=tokens.NAV_ICON, dark=self._dark)
        mode = (QIcon.Mode.Selected if self.is_active else QIcon.Mode.Normal)
        self.icon_label.setPixmap(
            icon.pixmap(tokens.NAV_ICON, tokens.NAV_ICON, mode))

    def set_dark(self, dark: bool) -> None:
        """主题切换时重画图标（QIcon 是 QPixmap，不像 QSS 那样自动跟随主题）。"""
        if self._dark == dark:
            return
        self._dark = dark
        self._refresh_icon()

    def set_active(self, active: bool) -> None:
        value = "true" if active else "false"
        set_prop(self, "active", value)
        set_prop(self.indicator, "active", value)
        set_prop(self.title, "active", value)
        set_prop(self.sub, "active", value)
        self._refresh_icon()

    @property
    def is_active(self) -> bool:
        return self.property("active") == "true"

    # ------------------------------------------------------------------
    def mouseReleaseEvent(self, event) -> None:  # noqa: N802 (Qt 命名)
        # 用 release + 命中判断，而不是 press：按下后拖出去再松开应当取消，
        # 这是按钮的标准手感（press 触发会在拖动时误跳页）。
        if (event.button() == Qt.MouseButton.LeftButton
                and self.rect().contains(event.position().toPoint())):
            self.activated.emit(self.key)
        super().mouseReleaseEvent(event)

    def keyPressEvent(self, event) -> None:  # noqa: N802
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Space):
            self.activated.emit(self.key)
            return
        super().keyPressEvent(event)

    def focusInEvent(self, event) -> None:  # noqa: N802
        # 键盘走到哪一行，就把那一行滚进视野
        super().focusInEvent(event)
        self._ensure_visible()

    def _ensure_visible(self) -> None:
        scroll = self.parent()
        while scroll is not None and not isinstance(scroll, QScrollArea):
            scroll = scroll.parent()
        if isinstance(scroll, QScrollArea):
            scroll.ensureWidgetVisible(self, 0, 8)


class NavList(QScrollArea):
    """可滚动导航列表。

    条目不足时（大屏）底部留白由末尾的 ``addStretch(1)`` 吃掉；
    条目过多时（小窗口 / 管理员）出现的滚动条是**按需显示**且可操作的 ——
    这正是修复「管理员在 1080×700 下够不到『管理后台』」的关键：
    原先 ``ScrollBarAlwaysOff``，内容溢出后多出来的部分**物理上无法触及**。
    """

    navigated = Signal(str)

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setObjectName("NavScroll")
        self.setWidgetResizable(True)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)

        self._body = QWidget()
        self._body.setObjectName("NavBody")
        self._lay = QVBoxLayout(self._body)
        self._lay.setContentsMargins(
            tokens.NAV_SIDE_PAD, 4, tokens.NAV_SIDE_PAD, 10)
        self._lay.setSpacing(tokens.NAV_ROW_GAP)
        # 末尾弹性：条目总高小于可视高度时，多出来的空间沉到底部，
        # 而不是把每条均匀拉开（会被误读成「行高不一致」）。
        self._lay.addStretch(1)
        self.setWidget(self._body)

        self._rows: Dict[str, NavRow] = {}
        self._group_labels: Dict[str, QLabel] = {}
        self._active: Optional[str] = None
        self._dark = tokens.is_dark()

    # ------------------------------------------------------------------
    # 构建
    # ------------------------------------------------------------------
    def _append(self, widget: QWidget) -> None:
        """插到末尾弹性项之前（否则会被塞到留白下面看不见）。"""
        self._lay.insertWidget(self._lay.count() - 1, widget)

    def add_group(self, group: NavGroup, first: bool = False) -> QLabel:
        if group.key in self._group_labels:
            return self._group_labels[group.key]
        label = QLabel(group.label, self._body)
        label.setObjectName("NavGroupLabel")
        # 高度交给 QSS 的 padding 决定（分组上方的呼吸空间来自 padding-top）。
        # 不用 setFixedHeight：固定高 + 字体度量变化会挤掉文字，
        # 而 QSS padding 会随字体自动撑开，不会截断。
        label.setAlignment(
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        label.setProperty("first", "true" if first else "false")
        self._append(label)
        self._group_labels[group.key] = label
        return label

    def add_item(self, item: NavItem) -> NavRow:
        existing = self._rows.get(item.key)
        if existing is not None:
            return existing
        row = NavRow(item, self._body)
        row.activated.connect(self._on_row_activated)
        self._append(row)
        self._rows[item.key] = row
        return row

    def build(self, groups, *, first_group_is_first: bool = True) -> None:
        """一次性铺出所有分组（幂等：已存在的行会被复用）。"""
        for idx, group in enumerate(groups):
            self.add_group(group, first=(first_group_is_first and idx == 0))
            for item in group.items:
                self.add_item(item)

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------
    def keys(self):
        return tuple(self._rows.keys())

    def row(self, key: str) -> Optional[NavRow]:
        return self._rows.get(key)

    @property
    def active_key(self) -> Optional[str]:
        return self._active

    @property
    def row_count(self) -> int:
        """可点击条目数（不含分组标题 —— 分组标题不是导航项）。"""
        return len(self._rows)

    def group_labels(self):
        """分组标题文本（按加入顺序）。供测试与体检脚本断言分组结构。"""
        return tuple(self._group_labels[g].text() for g in self._group_labels)

    # ------------------------------------------------------------------
    # 选中
    # ------------------------------------------------------------------
    def set_active(self, key: str, *, emit: bool = False) -> bool:
        """设置高亮项。返回是否命中（未注册的 key 返回 False，不抛错）。

        ``emit=True`` 时额外发 ``navigated``（点击路径用）；
        程序化切换（启动落地、``navigate()``）用 ``emit=False`` 避免回环。
        """
        if key not in self._rows:
            return False
        if self._active != key:
            old = self._rows.get(self._active or "")
            if old is not None:
                old.set_active(False)
            self._active = key
            self._rows[key].set_active(True)
            self.scrollTo(self._rows[key])
        if emit:
            self.navigated.emit(key)
        return True

    def scrollTo(self, row: NavRow) -> None:  # noqa: N802 (与 QAbstractItemView 同名)
        self.ensureWidgetVisible(row, 0, 8)

    def set_dark(self, dark: bool) -> None:
        self._dark = dark
        for row in self._rows.values():
            row.set_dark(dark)

    def _on_row_activated(self, key: str) -> None:
        self.set_active(key, emit=True)


class NavBrand(QWidget):
    """侧栏品牌区：渐变方块 + 产品名 + 一句话定位。

    改造前是「OfferFlow + 桌面版徽标 + 求职全流程智能工作台」三件散装控件，
    其中徽标要挤在同一条右对齐，宽度一紧就先被压扁。现在把「桌面版」
    下沉到版本行（见 ``SidebarVersion``），品牌区只留 mark + 名称 + 副标题。
    """

    def __init__(self, app_name: str, subtitle: str,
                 parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setObjectName("NavBrand")

        lay = QHBoxLayout(self)
        lay.setContentsMargins(14, 16, 14, 14)
        lay.setSpacing(10)

        self.mark = QLabel("OF", self)
        self.mark.setObjectName("NavBrandMark")
        self.mark.setFixedSize(34, 34)
        self.mark.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(self.mark, 0, Qt.AlignmentFlag.AlignVCenter)

        col = QVBoxLayout()
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(0)
        self.name = QLabel(app_name, self)
        self.name.setObjectName("NavBrandName")
        self.sub = QLabel(subtitle, self)
        self.sub.setObjectName("NavBrandSub")
        self.sub.setWordWrap(False)
        col.addWidget(self.name)
        col.addWidget(self.sub)
        lay.addLayout(col, 1)


class NavUserCard(QFrame):
    """侧栏底部用户卡：头像 + 昵称 + 角色 + 退出。

    为什么放到侧栏：改造前「退出登录」只存在于「系统设置」页里，
    要退出得先想得到去系统设置找 —— 这是网页版早就修掉的可用性问题
    （网页版侧栏底部就有头像 + 角色 + 退出）。桌面端一直缺这块。

    点击卡片主体 → 去「系统设置」（改昵称 / 换头像 / 改密码都在那儿）。
    """

    logout_requested = Signal()
    profile_requested = Signal()

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setObjectName("NavUserCard")
        self.setCursor(Qt.CursorShape.PointingHandCursor)

        lay = QHBoxLayout(self)
        lay.setContentsMargins(14, 10, 10, 10)
        lay.setSpacing(10)

        self.avatar = QLabel("—", self)
        self.avatar.setObjectName("NavAvatar")
        self.avatar.setFixedSize(tokens.NAV_AVATAR, tokens.NAV_AVATAR)
        self.avatar.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(self.avatar, 0, Qt.AlignmentFlag.AlignVCenter)

        col = QVBoxLayout()
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(0)
        self.name = QLabel("未登录", self)
        self.name.setObjectName("NavUserName")
        self.role = QLabel("", self)
        self.role.setObjectName("NavUserRole")
        col.addWidget(self.name)
        col.addWidget(self.role)
        lay.addLayout(col, 1)

        self.logout_btn = QPushButton("退出", self)
        self.logout_btn.setObjectName("NavLogout")
        self.logout_btn.setProperty("class", "navicon")
        self.logout_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.logout_btn.setFixedSize(44, 26)
        self.logout_btn.setToolTip("退出登录")
        self.logout_btn.clicked.connect(self.logout_requested.emit)
        lay.addWidget(self.logout_btn, 0, Qt.AlignmentFlag.AlignVCenter)

    def set_user(self, name: str, role: str) -> None:
        """更新显示。``name`` 为空时显示「未登录」，头像用名字首字（中文取首字，
        英文取首字母大写）—— 与网页版 Avatar 的回退策略一致，不依赖头像图片加载。"""
        display = (name or "").strip()
        self.name.setText(display or "未登录")
        self.name.setToolTip(display or "未登录")
        self.role.setText(role or "")
        initial = display[:1].upper() if display else "—"
        self.avatar.setText(initial)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if (event.button() == Qt.MouseButton.LeftButton
                and self.rect().contains(event.position().toPoint())):
            self.profile_requested.emit()
        super().mouseReleaseEvent(event)

