# -*- coding: utf-8 -*-
"""自绘图表控件（零第三方依赖）。

为什么不用 QtCharts：多引入一个 Qt 插件（``Qt6Charts.dll``）就多一份
**打包隐式依赖**（spec 要额外收集、离线环境缺了就是整个页面起不来），
而 QChart 自带一套主题体系，与本项目「图表配色随数据语义走、不参与
视觉规范收敛」（见 ``tokens.C`` 的注释）的约定也合不来。这里与
``ui/icons.py`` / ``ui/nav_list.py`` 走同一条路线：QPainter 手绘，
颜色按 ``tokens.is_dark()`` 取当前档位。

**手绘像素不跟 QSS 重刷** —— 切主题只做「setStyleSheet + 重绘侧栏图标」
（``main_window.apply_theme``），既不重建页面也不重画本模块的画布。
所以这里的控件都监听 ``QEvent.StyleChange`` 自我 ``update()``；
否则切到深色后图表仍是浅色，和 ``QCalendarWidget`` 那块「夹在深色里的
白板」是同一类问题。

几何计算与绘制**分开**（``ratios`` / ``slice_geometry`` / ``delta`` 不依赖 QPainter）：
判据可以断言数值（条长 ∝ 数值、扇区角 ∝ 数值），而不是只能对着像素猜。
"""

from __future__ import annotations

from typing import Any, Dict, List, Sequence, Tuple

from PySide6.QtCore import QEvent, QPointF, QRectF, QSize, Qt
from PySide6.QtGui import QColor, QFont, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QSizePolicy, QWidget

from . import tokens

#: 一条数据 = (标签, 数值, 颜色)
Item = Tuple[str, float, str]


def _fmt_ms(ms: float) -> str:
    """毫秒 → 人类可读。

    与成本页 ``cost.fmt_duration`` 同口径，但**不在本模块 import 页面模块** ——
    charts 是被页面依赖的底层，反过来依赖页面会成环。
    """
    v = float(ms or 0)
    if v < 1000:
        return f"{v:.0f}ms"
    if v < 60000:
        return f"{v / 1000:.1f}s"
    return f"{v // 60000:.0f}m{round(v % 60000 / 1000)}s"


def _palette() -> Dict[str, str]:
    """当前主题下的辅助色（文字 / 空槽 / 空态框）。

    只放**轴、文字、底槽**这类辅助色；数据色一律由调用方传入，
    因为它是「数据语义色」（STATUS_COLORS / STATUS_COLORS_DARK），
    不属于视觉规范。
    """
    dark = tokens.is_dark()
    return {
        "text": "#e4e4e7" if dark else tokens.C.TEXT,
        "sub": "#9a9aa3" if dark else tokens.C.TEXT_HINT,
        # 底槽要比卡片底**亮一档**，否则深色下条形的"量程"看不见
        # （卡片底 #26262f，槽 #33333f；浅色下卡片白底、槽 #f1f2f6）
        "track": "#33333f" if dark else tokens.C.BG_SUBTLE,
        "empty_border": "#3a3a46" if dark else tokens.C.BORDER,
        "empty_text": "#6b6b74" if dark else tokens.C.TEXT_MUTED,
    }


def _font(pixel: int, bold: bool = False) -> QFont:
    f = QFont()
    f.setPixelSize(pixel)
    f.setBold(bold)
    return f


def _paint_empty_box(painter: QPainter, text: str, radius: int,
                     rect: QRectF | None = None) -> None:
    """空态：虚线框 + 居中提示（对齐网页版 ChartEmpty，不留一整块空白）。

    ``rect`` 可指定框的位置与大小 —— 撑满整块画布（比如环形图那种
    Expanding 的控件）会显得空荡，居中收一个合适的高度更好看。
    """
    pal = _palette()
    if rect is None:
        rect = QRectF(0.5, 0.5, painter.device().width() - 1.0,
                      painter.device().height() - 1.0)
    pen = QPen(QColor(pal["empty_border"]))
    pen.setStyle(Qt.PenStyle.DashLine)
    pen.setWidthF(1.0)
    painter.setPen(pen)
    painter.setBrush(Qt.BrushStyle.NoBrush)
    painter.drawRoundedRect(rect, radius, radius)
    painter.setPen(QColor(pal["empty_text"]))
    painter.setFont(_font(tokens.FS_HINT))
    painter.drawText(
        rect.adjusted(10, 6, -10, -6),
        int(Qt.AlignmentFlag.AlignCenter | Qt.TextFlag.TextWordWrap), text)


class BarChart(QWidget):
    """横向条形图（投递漏斗）。

    行高固定、行距固定，高度由行数决定（``sizeHint``），所以在纵向布局里
    不会被压扁成一堆重叠的条。
    """

    ROW_H = 26        # 单行高度
    ROW_GAP = 8       # 行间距
    LABEL_W = 60      # 左侧标签列宽
    VALUE_W = 44      # 右侧数值列宽
    BAR_H = 12        # 条厚

    def __init__(self, empty_text: str = "暂无数据", parent=None) -> None:
        super().__init__(parent)
        self._items: List[Item] = []
        self._empty_text = empty_text
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    # ===== 数据 =====

    def set_items(self, items: Sequence[Item]) -> None:
        self._items = [(str(a), float(b), str(c)) for a, b, c in items]
        self.updateGeometry()
        self.update()

    def items(self) -> List[Item]:
        return list(self._items)

    def visible_items(self) -> List[Item]:
        """值为 0 的不画（对齐网页版 ``funnelData.filter(count > 0)``）。"""
        return [(lab, val, col) for lab, val, col in self._items if val > 0]

    def ratios(self) -> Dict[str, float]:
        """每个标签的条长占「满格」的比例，0~1。

        判据据此断言「条长 ∝ 数值」；最大值自身恒为 1.0，所以比值集合
        与值的集合同构（数值翻倍则比值不变，只改满格长度）。
        """
        vals = [v for _, v, _ in self.visible_items()]
        top = max(vals) if vals else 0.0
        if top <= 0:
            return {lab: 0.0 for lab, _, _ in self.visible_items()}
        return {lab: v / top for lab, v, _ in self.visible_items()}

    # ===== 尺寸 =====

    def sizeHint(self) -> QSize:
        n = len(self.visible_items())
        h = n * self.ROW_H + max(0, n - 1) * self.ROW_GAP
        return QSize(280, max(64, h))

    def minimumSizeHint(self) -> QSize:
        return self.sizeHint()

    # ===== 绘制 =====

    def changeEvent(self, event) -> None:               # noqa: N802
        # 手绘像素不跟 QSS 重刷（文件头有说明）
        if event.type() == QEvent.Type.StyleChange:
            self.update()
        super().changeEvent(event)

    def paintEvent(self, event) -> None:                # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        pal = _palette()
        rows = self.visible_items()
        if not rows:
            _paint_empty_box(painter, self._empty_text, tokens.R_LG)
            painter.end()
            return

        top = max(v for _, v, _ in rows)
        avail = max(1.0, self.width() - self.LABEL_W - self.VALUE_W - 6.0)
        painter.setFont(_font(tokens.FS_HINT))
        y = 0.0
        for label, value, color in rows:
            painter.setPen(QColor(pal["sub"]))
            painter.drawText(
                QRectF(0, y, self.LABEL_W - 8, self.ROW_H),
                int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight),
                label)

            bar_y = y + (self.ROW_H - self.BAR_H) / 2.0
            r = self.BAR_H / 2.0
            painter.setPen(Qt.PenStyle.NoPen)
            # 底槽：哪怕值为 0 也画出来，用户才知道「这一格是它的量程」
            painter.setBrush(QColor(pal["track"]))
            painter.drawRoundedRect(
                QRectF(self.LABEL_W, bar_y, avail, self.BAR_H), r, r)
            if value > 0:
                width = max(3.0, avail * (value / top))
                painter.setBrush(QColor(color))
                painter.drawRoundedRect(
                    QRectF(self.LABEL_W, bar_y, width, self.BAR_H), r, r)

            painter.setPen(QColor(pal["text"]))
            painter.drawText(
                QRectF(self.width() - self.VALUE_W, y, self.VALUE_W, self.ROW_H),
                int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft),
                f"{value:g}")
            y += self.ROW_H + self.ROW_GAP
        painter.end()


class ScoreCompare(QWidget):
    """优化前后评分对比（两条**同尺**横条 + 右侧数值）。

    与 :class:`BarChart` 的**关键差别在量程**：BarChart 拿本组最大值当满格
    （比的是相对大小），这里**恒以 100 为满格**。ATS 满分就是 100，两条
    必须同尺才能读出「涨了多少」；若按最大值归一，「52 → 100」会画成和
    「52 → 53」一样长的两根条 —— 那恰好是这块对比图的全部意义所在。

    ``set_scores(None, ...)`` 表示这项数据没有，整块退回空态虚线框
    （调用方据此也可以整块隐藏）。
    """

    ROW_H = 30
    ROW_GAP = 8
    LABEL_W = 60      # 左侧「优化前 / 优化后」
    VALUE_W = 52      # 右侧分数值
    BAR_H = 14        # 条厚
    FULL = 100.0      # 满格刻度（不是本组最大值）

    def __init__(self, empty_text: str = "还没有 ATS 对比数据", parent=None) -> None:
        super().__init__(parent)
        self._rows: List[Item] = []
        self._empty_text = empty_text
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    # ===== 数据 =====

    def set_scores(self, before, after, before_color: str = "#f59e0b",
                   after_color: str = "#16a34a",
                   before_label: str = "优化前", after_label: str = "优化后") -> None:
        def _clamp(x):
            if x is None:
                return None
            return max(0.0, min(self.FULL, float(x)))

        self._rows = [(before_label, _clamp(before), before_color),
                      (after_label, _clamp(after), after_color)]
        self.updateGeometry()
        self.update()

    def items(self) -> List[Item]:
        return list(self._rows)

    def has_data(self) -> bool:
        """两条分数都在才算「有数据」（任何一条缺失都退回空态）。"""
        return bool(self._rows) and all(v is not None for _lab, v, _c in self._rows)

    def ratios(self) -> Dict[str, float]:
        """条长占满格的比例 = 分数 / 100（量程固定，**不**按最大值归一）。"""
        return {lab: (0.0 if v is None else v / self.FULL) for lab, v, _c in self._rows}

    def delta(self) -> float | None:
        """优化后 − 优化前。任一条缺失返回 ``None``（不是 0）。"""
        if not self.has_data():
            return None
        values = [v for _lab, v, _c in self._rows]
        return values[1] - values[0]

    # ===== 尺寸 =====

    def sizeHint(self) -> QSize:
        n = len(self._rows) or 2
        h = n * self.ROW_H + max(0, n - 1) * self.ROW_GAP
        return QSize(300, max(52, h))

    def minimumSizeHint(self) -> QSize:
        return self.sizeHint()

    # ===== 绘制 =====

    def changeEvent(self, event) -> None:               # noqa: N802
        if event.type() == QEvent.Type.StyleChange:
            self.update()
        super().changeEvent(event)

    def paintEvent(self, event) -> None:                # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        pal = _palette()
        if not self.has_data():
            _paint_empty_box(painter, self._empty_text, tokens.R_LG)
            painter.end()
            return

        avail = max(1.0, self.width() - self.LABEL_W - self.VALUE_W - 8.0)
        r = self.BAR_H / 2.0
        y = 0.0
        for label, value, color in self._rows:
            painter.setPen(QColor(pal["sub"]))
            painter.setFont(_font(tokens.FS_HINT))
            painter.drawText(
                QRectF(0, y, self.LABEL_W - 8, self.ROW_H),
                int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight),
                label)

            bar_y = y + (self.ROW_H - self.BAR_H) / 2.0
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(pal["track"]))
            painter.drawRoundedRect(
                QRectF(self.LABEL_W, bar_y, avail, self.BAR_H), r, r)
            if value > 0:
                painter.setBrush(QColor(color))
                painter.drawRoundedRect(
                    QRectF(self.LABEL_W, bar_y,
                           max(r * 2, avail * (value / self.FULL)), self.BAR_H), r, r)

            # 分数走**大号加粗**，与条形互为印证（条形看相对、数字看绝对）
            painter.setPen(QColor(color))
            painter.setFont(_font(tokens.FS_CARD_TITLE, bold=True))
            painter.drawText(
                QRectF(self.width() - self.VALUE_W, y, self.VALUE_W, self.ROW_H),
                int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft),
                f"{value:g}")
            y += self.ROW_H + self.ROW_GAP
        painter.end()


class WaterfallChart(QWidget):
    """调用链瀑布图：横轴为时间，每个 span 一条（左偏移 = ``start_offset_ms``）。

    与 :class:`BarChart` 的**关键差别在横轴语义**：BarChart 的条一律从 0 起
    （比的是大小），这里每条从自己的开始时刻起（比的是时序与重叠）—— 所以
    几何必须同时暴露 ``left`` 与 ``width``，只给宽度就判不出「谁先谁后」。

    几何与绘制分离：:meth:`bars` 返回每条的 ``(模型名, 左占比, 宽占比, 是否失败)``，
    判据直接断言数值，不必截图取色。
    """

    ROW_H = 24            # 单条行高
    ROW_GAP = 6           # 行间距
    LABEL_W = 132         # 左侧模型名列宽
    VALUE_W = 56          # 右侧耗时列宽
    BAR_H = 14            # 条厚
    TICK_H = 18           # 顶部时间轴高度
    LEGEND_H = 18         # 底部图例高度
    #: 一条最短也要占这么宽 —— 对齐网页版 ``Math.max(width, 0.6)``（百分比）
    MIN_WIDTH_RATIO = 0.006
    #: 时间轴刻度位置（占比）
    TICKS: Tuple[float, ...] = (0.0, 0.25, 0.5, 0.75, 1.0)

    OK_COLOR_A = "#6366f1"    # 正常调用（偶数条）
    OK_COLOR_B = "#0ea5e9"    # 正常调用（奇数条，交替便于分辨相邻调用）
    FAIL_COLOR = "#f43f5e"    # 失败调用

    def __init__(self, empty_text: str = "暂无调用明细", parent=None) -> None:
        super().__init__(parent)
        self._spans: List[Dict[str, Any]] = []
        self._total_ms: float = 0.0
        self._empty_text = empty_text
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    # ===== 数据 =====

    def set_spans(self, spans: Sequence[Dict[str, Any]],
                  total_duration_ms: float = 0) -> None:
        self._spans = [dict(s) for s in (spans or []) if isinstance(s, dict)]
        self._total_ms = float(total_duration_ms or 0)
        self.updateGeometry()
        self.update()

    def spans(self) -> List[Dict[str, Any]]:
        return [dict(s) for s in self._spans]

    def total_ms(self) -> float:
        """横轴满格时长 = ``max(整体耗时, 各 span 的结束时刻, 1)``。

        取 1 是防除零；取「各 span 结束时刻」是因为 ``start_offset_ms`` 可能
        来自另一次 run（跨 run 的偏移之和会超出整体耗时），不兜底就会有条例
        被画到框外。
        """
        ends = [float(self._total_ms or 0)]
        for s in self._spans:
            ends.append(float(s.get("start_offset_ms") or 0)
                        + float(s.get("duration_ms") or 0))
        ends.append(1.0)
        return max(ends)

    @staticmethod
    def is_failed(status: Any) -> bool:
        """失败判定：``error`` / ``failed``（大小写与空白都容错）。"""
        return str(status or "").strip().lower() in ("error", "failed")

    def bars(self) -> List[Tuple[str, float, float, bool]]:
        """``[(模型名, 左占比 0~1, 宽占比 0~1, 是否失败)]``，顺序与 spans 一致。"""
        total = self.total_ms()
        out: List[Tuple[str, float, float, bool]] = []
        for s in self._spans:
            left = float(s.get("start_offset_ms") or 0) / total
            width = max(float(s.get("duration_ms") or 0) / total,
                        self.MIN_WIDTH_RATIO)
            out.append((str(s.get("model") or "未知"), left, width,
                        self.is_failed(s.get("status"))))
        return out

    def bar_color(self, index: int, failed: bool) -> str:
        """第 ``index`` 条的颜色（失败恒红，否则奇偶交替）。"""
        if failed:
            return self.FAIL_COLOR
        return self.OK_COLOR_A if index % 2 == 0 else self.OK_COLOR_B

    # ===== 尺寸 =====

    def sizeHint(self) -> QSize:
        n = len(self._spans)
        h = (self.TICK_H + 6 + n * self.ROW_H
             + max(0, n - 1) * self.ROW_GAP + self.LEGEND_H)
        return QSize(360, max(80, h))

    def minimumSizeHint(self) -> QSize:
        return self.sizeHint()

    # ===== 绘制 =====

    def changeEvent(self, event) -> None:               # noqa: N802
        # 手绘像素不跟 QSS 重刷（文件头有说明）
        if event.type() == QEvent.Type.StyleChange:
            self.update()
        super().changeEvent(event)

    def paintEvent(self, event) -> None:                # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        pal = _palette()
        rows = self.bars()
        if not rows:
            _paint_empty_box(painter, self._empty_text, tokens.R_LG)
            painter.end()
            return

        track_x = float(self.LABEL_W)
        track_w = max(1.0, self.width() - self.LABEL_W - self.VALUE_W - 8.0)
        total = self.total_ms()

        # 顶部时间轴：0 / 25% / 50% / 75% / 100%
        painter.setFont(_font(tokens.FS_TINY))
        for p in self.TICKS:
            x = track_x + track_w * p
            painter.setPen(QColor(pal["sub"]))
            painter.drawText(
                QRectF(x - 30.0, 0.0, 60.0, self.TICK_H),
                int(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter),
                _fmt_ms(total * p))
        painter.setPen(QPen(QColor(pal["empty_border"]), 1.0))
        painter.drawLine(QPointF(track_x, self.TICK_H - 0.5),
                         QPointF(track_x + track_w, self.TICK_H - 0.5))

        r = self.BAR_H / 2.0
        y = float(self.TICK_H) + 6.0
        for idx, ((label, left, width, failed), span) in enumerate(
                zip(rows, self._spans)):
            bar_y = y + (self.ROW_H - self.BAR_H) / 2.0

            # 轨道（量程）+ 4 等分网格线：没有轨道就看不出条「从哪起」
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(pal["track"]))
            painter.drawRoundedRect(
                QRectF(track_x, bar_y, track_w, self.BAR_H), r, r)
            painter.setPen(QPen(QColor(pal["empty_border"]), 1.0))
            for p in (0.25, 0.5, 0.75):
                gx = track_x + track_w * p
                painter.drawLine(QPointF(gx, bar_y),
                                 QPointF(gx, bar_y + self.BAR_H))

            # 时间条：起点 = start_offset，长度 = duration（最短两端圆角宽度）
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(self.bar_color(idx, failed)))
            painter.drawRoundedRect(
                QRectF(track_x + track_w * left, bar_y,
                       max(r * 2.0, track_w * width), self.BAR_H), r, r)

            # 左：模型名（右对齐、超宽省略）；右：耗时
            painter.setPen(QColor(pal["sub"]))
            painter.setFont(_font(tokens.FS_HINT))
            fm = painter.fontMetrics()
            painter.drawText(
                QRectF(0.0, y, self.LABEL_W - 8.0, self.ROW_H),
                int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight),
                fm.elidedText(label, Qt.TextElideMode.ElideRight,
                              int(self.LABEL_W - 8)))
            painter.drawText(
                QRectF(self.width() - self.VALUE_W, y, self.VALUE_W, self.ROW_H),
                int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft),
                _fmt_ms(span.get("duration_ms") or 0))
            y += self.ROW_H + self.ROW_GAP

        # 底部图例
        painter.setFont(_font(tokens.FS_TINY))
        legend_fm = painter.fontMetrics()
        lx = track_x + 2.0
        for color, text in ((self.OK_COLOR_A, "正常调用"),
                            (self.FAIL_COLOR, "失败")):
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(color))
            painter.drawEllipse(QPointF(lx + 4.0, y + self.LEGEND_H / 2.0), 4.0, 4.0)
            painter.setPen(QColor(pal["sub"]))
            painter.drawText(
                QRectF(lx + 12.0, y, 90.0, self.LEGEND_H),
                int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft),
                text)
            lx += 12.0 + legend_fm.horizontalAdvance(text) + 14.0
        painter.end()


class DonutChart(QWidget):
    """环形图（状态分布）+ 右侧图例，圆心显示总计。"""

    LEGEND_W = 136
    PAD = 6
    LEGEND_ROW_H = 18

    def __init__(self, empty_text: str = "暂无数据", parent=None) -> None:
        super().__init__(parent)
        self._items: List[Item] = []
        self._empty_text = empty_text
        self._total_caption = "总计"
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

    # ===== 数据 =====

    def set_items(self, items: Sequence[Item]) -> None:
        self._items = [(str(a), float(b), str(c)) for a, b, c in items]
        self.update()

    def items(self) -> List[Item]:
        return list(self._items)

    def visible_items(self) -> List[Item]:
        return [(lab, val, col) for lab, val, col in self._items if val > 0]

    def total(self) -> float:
        return sum(v for _, v, _ in self.visible_items())

    def slice_geometry(self) -> List[Tuple[str, float, float, str, float]]:
        """[(标签, 起始角, 张角, 颜色, 数值)]，角度制、从 12 点起**顺时针**。

        起始角 90°（Qt 里 90° 是 12 点方向），每片按数值占比分 360°，
        起点沿着顺时针（角度递减）推进。
        """
        rows = self.visible_items()
        total = self.total()
        if total <= 0:
            return []
        out: List[Tuple[str, float, float, str, float]] = []
        start = 90.0
        for label, value, color in rows:
            span = 360.0 * value / total
            out.append((label, start, span, color, value))
            start -= span
        return out

    # ===== 尺寸 =====

    def sizeHint(self) -> QSize:
        return QSize(320, 200)

    def minimumSizeHint(self) -> QSize:
        return QSize(240, 150)

    # ===== 绘制 =====

    def changeEvent(self, event) -> None:               # noqa: N802
        if event.type() == QEvent.Type.StyleChange:
            self.update()
        super().changeEvent(event)

    def paintEvent(self, event) -> None:                # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        pal = _palette()
        geom = self.slice_geometry()
        if not geom:
            # 空态框限高居中：本控件是 Expanding 的，撑满整块会显得空荡
            box_h = min(float(self.height()) - 2.0 * self.PAD, 180.0)
            _paint_empty_box(
                painter, self._empty_text, tokens.R_LG,
                QRectF(0.5, (self.height() - box_h) / 2.0, self.width() - 1.0, box_h))
            painter.end()
            return

        avail_w = max(60.0, self.width() - self.LEGEND_W - 16.0)
        diameter = max(60.0, min(self.height() - 2.0 * self.PAD, avail_w, 260.0))
        outer = QRectF(self.PAD, (self.height() - diameter) / 2.0,
                       diameter, diameter)
        inner_d = diameter * 0.6
        center = outer.center()
        inner = QRectF(center.x() - inner_d / 2.0, center.y() - inner_d / 2.0,
                       inner_d, inner_d)

        # 用「外圆 − 内圆」的路径做裁剪，比「画完再用背景色盖一个圆」可靠：
        # 后者要猜卡片底色，换个主题就露馅。
        ring = QPainterPath()
        ring.addEllipse(outer)
        hole = QPainterPath()
        hole.addEllipse(inner)
        ring = ring.subtracted(hole)

        painter.save()
        painter.setClipPath(ring)
        painter.setPen(Qt.PenStyle.NoPen)
        for _label, start, span, color, _value in geom:
            painter.setBrush(QColor(color))
            painter.drawPie(outer, int(round(start * 16)), int(round(-span * 16)))
        painter.restore()

        # 圆心：总计
        total = self.total()
        painter.setPen(QColor(pal["text"]))
        painter.setFont(_font(18, bold=True))
        painter.drawText(
            QRectF(center.x() - diameter / 2.0, center.y() - 20.0, diameter, 22),
            int(Qt.AlignmentFlag.AlignCenter), f"{total:g}")
        painter.setPen(QColor(pal["sub"]))
        painter.setFont(_font(tokens.FS_TINY))
        painter.drawText(
            QRectF(center.x() - diameter / 2.0, center.y() + 2.0, diameter, 14),
            int(Qt.AlignmentFlag.AlignCenter), self._total_caption)

        # 图例
        lx = outer.right() + 12.0
        n = len(geom)
        row_h = min(self.LEGEND_ROW_H, max(12.0, (self.height() - 8.0) / max(1, n)))
        ly = max(2.0, (self.height() - n * row_h) / 2.0)
        for label, _start, span, color, value in geom:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(color))
            painter.drawRoundedRect(QRectF(lx, ly + row_h / 2.0 - 4.5, 9, 9), 2, 2)
            painter.setPen(QColor(pal["sub"]))
            painter.setFont(_font(tokens.FS_HINT))
            painter.drawText(
                QRectF(lx + 15.0, ly, self.LEGEND_W - 15.0 - 40.0, row_h),
                int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft),
                label)
            painter.setPen(QColor(pal["text"]))
            painter.drawText(
                QRectF(lx + self.LEGEND_W - 40.0, ly, 40.0, row_h),
                int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight),
                f"{span / 360.0 * 100.0:.0f}%")
            ly += row_h
        painter.end()
