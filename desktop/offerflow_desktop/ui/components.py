"""通用 UI 组件：卡片、标签行、评分环、加载遮罩等。"""

from typing import Iterable, Optional

from PySide6.QtCore import QPoint, QRect, QSize, Qt, QTimer
from PySide6.QtGui import QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import (
    QFrame, QHBoxLayout, QLabel, QLayout, QProgressBar, QPushButton,
    QSizePolicy, QVBoxLayout, QWidget, QComboBox, QScrollArea, QGridLayout,
)

from ..config import STATUS_COLORS, STATUS_LABELS


def confirm_placeholders(parent, text: str) -> bool:
    """导出前检查未填的 [x] 占位符。

    生成/优化时「缺数字又不想编造」的地方会留 [x]（见 prompts._PLACEHOLDER_RULE），
    但用户很容易忘记补，直接导出就会把 [x] 印进成品。这里拦一道，
    默认按钮是「先去补」，避免手快点过。

    返回 True 表示可以继续导出。
    """
    from PySide6.QtWidgets import QMessageBox

    from ..core.text_utils import count_placeholders, find_placeholders

    n = count_placeholders(text)
    if not n:
        return True
    samples = find_placeholders(text)[:5]
    body = "\n".join(
        [f"简历里还有 {n} 处待填数字（占位符 [x]），例如：", ""]
        + ["· " + s for s in samples]
        + ["", "直接导出会把 [x] 原样写进成品，建议先补齐再导出。"]
    )
    ans = QMessageBox.warning(
        parent, "还有待填数字", body,
        QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        QMessageBox.StandardButton.No,
    )
    return ans == QMessageBox.StandardButton.Yes


def api_guard(fn):
    """把 `api_client.ApiError` 归一成 `LLMError`，供 `LLMWorker` 使用。

    `LLMWorker` 只对 `LLMError` 原样透传消息，其它异常会加「执行出错：」前缀；
    而「验证码错误」「Key 填错了」这类**可预期**的提示不该带那个前缀
    （那个前缀是留给意外崩溃的）。
    """
    from .. import api_client as api
    from ..llm import LLMError

    def wrapper():
        try:
            return fn()
        except api.ApiError as exc:
            raise LLMError(str(exc)) from exc

    return wrapper


def repolish(widget) -> None:
    """改完动态属性（``setProperty``）后强制重刷 QSS。

    为什么必须要有：Qt 的 QSS 只在属性变化**触发样式重算**时才重新求值。
    ``setProperty`` 本身不会触发重算，于是 ``QLabel[class="hint"]`` 这类
    属性选择器看起来「改了没反应」—— 实际是样式根本没重新匹配。
    unpolish + polish 才是让它生效的写法。
    """
    style = widget.style()
    style.unpolish(widget)
    style.polish(widget)
    widget.update()


def set_prop(widget, name: str, value: str) -> None:
    """设置动态属性并立即重刷样式（属性值统一用字符串，避免 Qt 的 bool 转换歧义）。"""
    if widget.property(name) == value:
        return
    widget.setProperty(name, value)
    repolish(widget)


def card(highlight: bool = False, warn: bool = False, danger: bool = False,
         success: bool = False) -> QFrame:
    frame = QFrame()
    cls = "card"
    if highlight:
        cls = "card-highlight"
    elif warn:
        cls = "card-warn"
    elif danger:
        cls = "card-danger"
    elif success:
        cls = "card-success"
    frame.setProperty("class", cls)
    # 竖直方向必须是 Preferred 而不是 Maximum：
    # Maximum 会让卡片永远长不过 sizeHint，父布局给它的拉伸空间就全变成「卡片之间的空白」。
    frame.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Preferred)
    return frame


def make_title(text: str, desc: str = "") -> QWidget:
    """页面标题 + 描述。"""
    w = QWidget()
    layout = QVBoxLayout(w)
    layout.setContentsMargins(0, 0, 0, 8)
    title = QLabel(text)
    title.setObjectName("PageTitle")
    layout.addWidget(title)
    if desc:
        d = QLabel(desc)
        d.setObjectName("PageDesc")
        d.setWordWrap(True)
        layout.addWidget(d)
    return w


def section_label(text: str) -> QLabel:
    label = QLabel(text)
    label.setProperty("class", "section-title")
    return label


def retrieval_summary(trace) -> str:
    """把「本次生成的召回轨迹」渲染成一句给人看的话（没有轨迹时返回空串）。

    为什么要在界面上显示：检索此前是**黑盒** —— 参考为空时用户只会猜「模型不行」，
    而真实原因常常是「本地知识库没建 / 已过期」或「语料里确实没有同岗位样本」。
    把「查了没、查到几条、来自哪里」摆出来，用户才答得上
    「每次生成到底有没有调用数据库」这个问题。
    """
    if not isinstance(trace, dict) or not trace:
        return ""
    corpus_n = int(trace.get("corpus_hits") or 0)
    work_n = int(trace.get("work_hits") or 0)
    echo_n = int(trace.get("echo_dropped") or 0)
    if not corpus_n and not work_n:
        err = str(trace.get("error") or "").strip()
        if err:
            return "📚 本次未取到参考语料（%s）—— 生成结果不受影响。" % err[:60]
        return ("📚 本次没有召回到参考语料（本地知识库可能尚未建立或已过期，"
                "可到「本地知识库」重建索引）—— 生成结果不受影响。")
    parts = []
    if corpus_n:
        parts.append("共享范文 %d 条" % corpus_n)
    if work_n:
        parts.append("你的历史成果 %d 条" % work_n)
    extra = ""
    if echo_n:
        extra = "（另有 %d 条与本次输入几乎相同，已按回声丢弃）" % echo_n
    return "📚 本次已查本地知识库，参考了：" + " · ".join(parts) + extra


def hint_label(text: str, shrink: bool = False) -> QLabel:
    """说明文字。

    shrink=True 用于「窄容器里的长中文提示」：
    wordWrap 的 QLabel 的 minimumSizeHint 宽度 = **最长单词**的宽度，而中文没有空格，
    整句会被当成一个词 —— 一句 30 字的中文能把父容器最小宽撑到 400px 以上，
    在分栏/窄栏里直接顶出**横向滚动条**。把水平策略设为 Ignored 即可让宽度由父容器
    决定、文字自行换行（竖直仍是 Preferred，不会被压扁）。

    默认 False 以保持既有页面行为不变（水平布局里与按钮并排的短提示不该被压窄）。
    """
    label = QLabel(text)
    label.setProperty("class", "hint")
    label.setWordWrap(True)
    if shrink:
        label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
    return label


def empty_state(text: str, sub: str = "") -> QWidget:
    """结果面板的空状态：内容垂直居中。

    卡片被撑满后如果内部什么都不放，就是一大片无意义的空白。
    这里给一句明确的引导语，让空白变成「有意留白」。
    """
    w = QWidget()
    lay = QVBoxLayout(w)
    lay.setContentsMargins(0, 0, 0, 0)
    lay.setSpacing(8)
    lay.addStretch(1)
    main = QLabel(text)
    main.setAlignment(Qt.AlignmentFlag.AlignCenter)
    main.setWordWrap(True)
    main.setProperty("class", "hint")
    main.setStyleSheet("font-size: 13px;")
    lay.addWidget(main)
    if sub:
        s = QLabel(sub)
        s.setAlignment(Qt.AlignmentFlag.AlignCenter)
        s.setWordWrap(True)
        s.setProperty("class", "muted")
        lay.addWidget(s)
    lay.addStretch(1)
    return w


def soft_chip(text: str, color: str | None = None) -> QLabel:
    """半透明同色底 + 同色文字的标签（安全版）。

    ⚠ 不要用 `f"{color}1a"` 拼法：Qt QSS 把 8 位十六进制当作
    `#AARRGGBB` 解析而不是 `#RRGGBBAA`，色相会完全跑偏（底色发绿）。
    这里用 rgba() 显式表达 A 通道，保证颜色即所见。

    color 省略时按当前主题取默认蓝 —— 内联优先级高于 QSS，
    写死浅蓝会让深色主题下的标签对比度掉到 2.67:1。
    """
    from .tokens import is_dark
    if color is None:
        color = "#a5b4fc" if is_dark() else "#1d4ed8"
    c = (color or "#1d4ed8").lstrip("#")
    r, g, b = int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16)
    label = QLabel(text)
    label.setStyleSheet(
        f"background: rgba({r},{g},{b},0.10); color: {color};"
        f"border: 1px solid rgba({r},{g},{b},0.35);"
        "border-radius: 10px; padding: 2px 10px; font-size: 12px;"
    )
    label.setAlignment(Qt.AlignmentFlag.AlignCenter)
    return label


def tag_label(text: str, color: str | None = None) -> QLabel:
    """标签（= soft_chip）。

    历史上 tag_label 用 `f"{color}1a"` 拼 8 位十六进制导致底色发绿，
    soft_chip 才是安全实现。统一视觉规范时把两者合并：tag_label 直接委托过去，
    避免同一个「标签」存在两套渲染结果。
    """
    return soft_chip(text, color)


def tone_color(ok: bool = True) -> str:
    """「成功/失败」语义色的主题感知取值。

    内联样式不跟随 QSS 重刷，写死浅色会让深色主题下的
    红字（#dc2626）只剩 3.70:1、绿字（#059669）同样不足。
    """
    from .tokens import is_dark
    if is_dark():
        return "#4ade80" if ok else "#f87171"
    return "#059669" if ok else "#dc2626"


def set_result_message(w: QLabel, ok: bool, text: str) -> None:
    """统一的「操作结果提示」：绿=成功 / 红=失败 + 10% 同色底。

    原先 change_email / forgot_password / profile_dialogs / model_settings
    **各自复制了一遍**同样的 rgba 拼色逻辑，且颜色写死浅色值。统一到这里后
    既消除重复，也让深色主题可读。
    """
    color = tone_color(ok)
    r, g, b = (int(color[1:3], 16), int(color[3:5], 16), int(color[5:7], 16))
    w.setStyleSheet(
        f"background: rgba({r},{g},{b},0.10); color: {color};"
        f"border: 1px solid rgba({r},{g},{b},0.30); border-radius: 8px;"
        "padding: 8px 12px; font-size: 12px;"
    )
    w.setText(text)
    w.show()


def primary_button(text: str) -> QPushButton:
    btn = QPushButton(text)
    btn.setProperty("class", "primary")
    btn.setCursor(Qt.CursorShape.PointingHandCursor)
    return btn


def success_button(text: str) -> QPushButton:
    btn = QPushButton(text)
    btn.setProperty("class", "success")
    btn.setCursor(Qt.CursorShape.PointingHandCursor)
    return btn


def danger_button(text: str) -> QPushButton:
    btn = QPushButton(text)
    btn.setProperty("class", "danger")
    btn.setCursor(Qt.CursorShape.PointingHandCursor)
    return btn


def score_badge(score: int) -> QLabel:
    """评分区间徽章。

    ⚠ 原先是内联 `color: #059669/#d97706/#dc2626` —— 内联优先级高于 QSS，
    切到深色主题时这些浅色值**依旧生效**，实测对比度仅 2.67:1（不可读）。
    改为语义 class 后由 QSS 统一供给，深浅主题各自校准。
    """
    if score >= 80:
        cls = "score-good"
    elif score >= 60:
        cls = "score-mid"
    else:
        cls = "score-bad"
    label = QLabel(f"{score}")
    label.setProperty("class", cls)
    return label


def status_chip(status: str) -> QLabel:
    """投递状态徽章。

    状态色是**数据语义色**（各状态要能互相区分），不能收敛成少数几个语义 class，
    因此保留内联配色，但按当前主题选用浅/深两套色板（见 config.STATUS_COLORS*），
    否则深色下蓝/紫/粉等状态色对比度不足 3.5:1。
    """
    from ..config import STATUS_COLORS, STATUS_COLORS_DARK
    from .tokens import is_dark
    table = STATUS_COLORS_DARK if is_dark() else STATUS_COLORS
    color = table.get(status, "#9ca3af" if is_dark() else "#94a3b8")
    label = QLabel(STATUS_LABELS.get(status, status))
    label.setStyleSheet(
        f"background: {color}1e; color: {color}; border-radius: 9px;"
        "padding: 2px 10px; font-size: 12px; font-weight: 600;"
    )
    label.setAlignment(Qt.AlignmentFlag.AlignCenter)
    return label


class FlowLayout(QLayout):
    """自动换行的流式布局（Qt 官方 FlowLayout 的 PySide6 版）。

    固定列数的 QGridLayout 会让「最小宽度 = 各列最小值之和」，
    标签一多就把父容器硬撑宽（出现横向滚动条）或留下大片右侧空白。
    流式布局按可用宽度换行，宽度自适应。
    """

    def __init__(self, parent=None, margin: int = 0,
                 h_spacing: int = 6, v_spacing: int = 6):
        super().__init__(parent)
        self._items = []
        self._h = h_spacing
        self._v = v_spacing
        self.setContentsMargins(margin, margin, margin, margin)

    # --- QLayout 必须实现的接口 ---
    def addItem(self, item):
        self._items.append(item)

    def count(self):
        return len(self._items)

    def itemAt(self, index):
        if 0 <= index < len(self._items):
            return self._items[index]
        return None

    def takeAt(self, index):
        if 0 <= index < len(self._items):
            return self._items.pop(index)
        return None

    def expandingDirections(self):
        return Qt.Orientation(0)

    def hasHeightForWidth(self):
        return True

    def heightForWidth(self, width: int) -> int:
        return self._do_layout(QRect(0, 0, width, 0), test_only=True)

    def setGeometry(self, rect):
        super().setGeometry(rect)
        self._do_layout(rect, test_only=False)

    def sizeHint(self):
        return self.minimumSize()

    def minimumSize(self):
        size = QSize()
        for item in self._items:
            size = size.expandedTo(item.minimumSize())
        m = self.contentsMargins()
        size += QSize(m.left() + m.right(), m.top() + m.bottom())
        return size

    def _do_layout(self, rect, test_only: bool) -> int:
        m = self.contentsMargins()
        area = rect.adjusted(m.left(), m.top(), -m.right(), -m.bottom())
        x, y, line_h = area.x(), area.y(), 0
        for item in self._items:
            hint = item.sizeHint()
            next_x = x + hint.width() + self._h
            if next_x - self._h > area.right() and line_h > 0:
                x = area.x()
                y = y + line_h + self._v
                next_x = x + hint.width() + self._h
                line_h = 0
            if not test_only:
                item.setGeometry(QRect(QPoint(x, y), hint))
            x = next_x
            line_h = max(line_h, hint.height())
        return y + line_h - rect.y() + m.bottom()


class FlowWidget(QWidget):
    """承载 FlowLayout 的容器。

    必须把 sizePolicy 的 heightForWidth 打开，父布局才会按实际换行行数给高度，
    否则控件会被压缩成一行高、内容互相重叠。
    """

    def __init__(self, parent=None, h_spacing: int = 6, v_spacing: int = 6):
        super().__init__(parent)
        # 纯布局容器：显式声明透明。全局 `QWidget { background }` 会给它刷上
        # 页面底色，放进白色卡片里就是一块灰（theme 里 `flush` 有说明）。
        self.setProperty("class", "flush")
        self._flow = FlowLayout(self, 0, h_spacing, v_spacing)
        pol = self.sizePolicy()
        pol.setHeightForWidth(True)
        self.setSizePolicy(pol)

    def add(self, widget: QWidget) -> None:
        self._flow.addWidget(widget)

    def add_many(self, widgets) -> None:
        for w in widgets:
            self._flow.addWidget(w)

    def clear(self) -> None:
        while self._flow.count():
            item = self._flow.takeAt(0)
            w = item.widget()
            if w is not None:
                w.setParent(None)
                w.deleteLater()

    def count(self) -> int:
        return self._flow.count()

    def widgets(self) -> list:
        """按加入顺序返回子控件。

        判据要用（比如「三种状态是不是三种颜色」）—— 有个公开取数口，
        测试就不必去碰 ``_flow`` 这个私有布局。
        """
        out = []
        for i in range(self._flow.count()):
            w = self._flow.itemAt(i).widget()
            if w is not None:
                out.append(w)
        return out

    def hasHeightForWidth(self):  # noqa: D102
        return True

    def heightForWidth(self, width: int) -> int:  # noqa: D102
        return self._flow.heightForWidth(width)


def flow_tags(items: Iterable[str], color: str = "#1d4ed8",
              max_count: int = 40) -> QWidget:
    """标签流式布局容器（自动换行，不再固定 6 列）。"""
    w = FlowWidget()
    w.add_many(tag_label(str(item), color) for item in list(items)[:max_count])
    return w


def combo_with_placeholder(combo: QComboBox, placeholder: str) -> QComboBox:
    combo.addItem(placeholder, "")
    return combo


def set_combo_data(combo: QComboBox, data: str) -> None:
    for i in range(combo.count()):
        if combo.itemData(i) == data:
            combo.setCurrentIndex(i)
            return


class BusyOverlay(QWidget):
    """半透明加载遮罩，盖在父控件上。"""

    def __init__(self, parent: QWidget, text: str = "AI 处理中，请稍候…"):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet("background: rgba(244,246,250,0.88);")
        layout = QVBoxLayout(self)
        self._label = QLabel(text)
        self._label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._label.setStyleSheet("font-size: 15px; font-weight: 600; color: #6366f1;")
        bar = QProgressBar()
        bar.setRange(0, 0)
        bar.setFixedWidth(260)
        layout.addStretch(1)
        layout.addWidget(self._label, 0, Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(bar, 0, Qt.AlignmentFlag.AlignCenter)
        layout.addStretch(1)
        self.hide()

    def set_text(self, text: str) -> None:
        self._label.setText(text)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self.parentWidget():
            self.setGeometry(self.parentWidget().rect())

    def show_over(self, text: Optional[str] = None) -> None:
        if text:
            self.set_text(text)
        if self.parentWidget():
            self.setGeometry(self.parentWidget().rect())
        self.raise_()
        self.show()


def scroll_content(layout) -> QScrollArea:
    """把一个布局包进滚动区域。"""
    inner = QWidget()
    inner.setLayout(layout)
    scroll = QScrollArea()
    scroll.setWidget(inner)
    scroll.setWidgetResizable(True)
    return scroll


# ============================================================
# 统计卡片 / 流程步骤（克制风格，白底 + 单一强调色）
# ============================================================

def stat_card(title: str, value: str, accent: str = "#4f46e5", sub: str = "") -> QFrame:
    """克制统计卡：白底 + 左侧彩色竖条点缀 + 深色大数字。

    返回的 QFrame 上**挂了三个引用**：``value_label`` / ``sub_label`` /
    ``accent_bar``。统计数字需要原地刷新（切页、拖拽改状态、手动刷新），
    而「整张卡片重建」会丢掉它在网格里的位置、界面闪一下，调用方还得自己
    记着格子坐标。子控件仍是普通 QLabel，QSS 与主题切换照旧生效。
    """
    frame = QFrame()
    frame.setProperty("class", "card")
    frame.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Preferred)
    layout = QHBoxLayout(frame)
    layout.setContentsMargins(16, 14, 16, 14)
    layout.setSpacing(12)
    bar = QFrame()
    bar.setFixedWidth(3)
    bar.setStyleSheet(f"background: {accent}; border: none; border-radius: 2px;")
    text_col = QVBoxLayout()
    text_col.setSpacing(4)
    t = QLabel(title)
    t.setProperty("class", "hint")
    v = QLabel(value)
    v.setStyleSheet("font-size: 24px; font-weight: 700;")
    text_col.addWidget(t)
    text_col.addWidget(v)
    frame.accent_bar = bar
    frame.value_label = v
    frame.sub_label = None
    if sub:
        s = QLabel(sub)
        s.setProperty("class", "muted")
        text_col.addWidget(s)
        frame.sub_label = s
    layout.addWidget(bar)
    layout.addLayout(text_col)
    return frame


def step_dot(index: int, title: str, active: bool = False, done: bool = False) -> QWidget:
    """流程步骤圆点 + 标题（用于首页求职流程引导条）。"""
    w = QWidget()
    layout = QHBoxLayout(w)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(8)
    if done:
        dot_style = "background: #4f46e5; color: #ffffff;"
        dot_text = "✓"
    else:
        dot_style = "background: #eef0fe; color: #4f46e5; border: 1px solid #d5d8f5;"
        dot_text = str(index)
    dot = QLabel(dot_text)
    dot.setFixedSize(20, 20)
    dot.setAlignment(Qt.AlignmentFlag.AlignCenter)
    dot.setStyleSheet(
        f"{dot_style} border-radius: 10px; font-size: 11px; font-weight: 700;"
    )
    label = QLabel(title)
    if active or done:
        label.setStyleSheet("font-size: 12px; font-weight: 600;")
    else:
        label.setProperty("class", "muted")
    layout.addWidget(dot)
    layout.addWidget(label)
    return w
