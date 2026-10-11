"""统一的线性矢量图标（Lucide 风格），供侧栏导航使用。

用 QPainter 直接手绘（依赖 QtGui 核心，打包稳定，无 QtSvg 插件依赖）。
- Normal 态 = 柔和灰（未选中）
- Selected 态 = 纯白（选中，落在渐变高亮背景上）
"""

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import (
    QBrush, QColor, QIcon, QLinearGradient, QPainter, QPainterPath, QPen, QPixmap,
)

_COLOR_NORMAL = "#6b7280"    # 未选中：中性灰
_COLOR_SELECTED = "#4f46e5"  # 选中：主色（白底侧栏上）
_PEN_WIDTH = 1.9


def _home(p: QPainter):
    p.drawPolyline([QPointF(3, 10.5), QPointF(12, 3), QPointF(21, 10.5)])
    p.drawPolyline([QPointF(5, 9.5), QPointF(5, 20), QPointF(19, 20), QPointF(19, 9.5)])


def _settings(p: QPainter):
    for y, cx in ((6, 9), (12, 15), (18, 7)):
        p.drawLine(QPointF(4, y), QPointF(20, y))
        p.drawEllipse(QPointF(cx, y), 2, 2)


def _resume(p: QPainter):
    path = QPainterPath()
    path.moveTo(14, 3)
    path.lineTo(14, 8)
    path.lineTo(19, 8)
    path.lineTo(19, 20)
    path.lineTo(5, 20)
    path.lineTo(5, 3)
    path.lineTo(14, 3)
    p.drawPath(path)
    p.drawLine(QPointF(9, 13), QPointF(15, 13))
    p.drawLine(QPointF(9, 17), QPointF(13, 17))


def _search(p: QPainter):
    p.drawEllipse(QPointF(11, 11), 7, 7)
    p.drawLine(QPointF(16, 16), QPointF(21, 21))


def _target(p: QPainter):
    p.drawEllipse(QPointF(12, 12), 9, 9)
    p.drawEllipse(QPointF(12, 12), 5, 5)
    p.drawEllipse(QPointF(12, 12), 1.5, 1.5)


def _edit(p: QPainter):
    path = QPainterPath()
    path.moveTo(17, 3)
    path.cubicTo(18.6, 3.6, 20.4, 5.4, 21, 7)
    path.lineTo(7.5, 20.5)
    path.lineTo(2, 22)
    path.lineTo(3.5, 16.5)
    path.closeSubpath()
    p.drawPath(path)


def _chat(p: QPainter):
    path = QPainterPath()
    path.moveTo(21, 11.5)
    path.cubicTo(21, 6, 17, 3, 12.5, 3)
    path.cubicTo(7.5, 3, 4, 6, 4, 11.5)
    path.cubicTo(4, 15.5, 5.5, 18, 7.5, 19.2)
    path.lineTo(3, 20.5)
    path.lineTo(4.6, 15.6)
    path.cubicTo(6, 19, 9, 20, 12.5, 20)
    path.cubicTo(17.5, 20, 21, 16, 21, 11.5)
    path.closeSubpath()
    p.drawPath(path)


def _banknote(p: QPainter):
    p.drawRoundedRect(QRectF(2, 6, 20, 12), 2, 2)
    p.drawEllipse(QPointF(12, 12), 2, 2)
    p.drawLine(QPointF(5.5, 12), QPointF(6.5, 12))
    p.drawLine(QPointF(17.5, 12), QPointF(18.5, 12))


def _clipboard(p: QPainter):
    p.drawRoundedRect(QRectF(8, 2, 8, 4), 1, 1)
    path = QPainterPath()
    path.moveTo(8, 4)
    path.lineTo(6, 4)
    path.lineTo(6, 20)
    path.lineTo(18, 20)
    path.lineTo(18, 4)
    path.lineTo(16, 4)
    p.drawPath(path)
    p.drawLine(QPointF(12, 11), QPointF(16, 11))
    p.drawLine(QPointF(12, 16), QPointF(16, 16))
    p.drawLine(QPointF(8, 11), QPointF(8.6, 11))


def _community(p: QPainter):
    """交流中心：两人并列（主人在前、同伴在后），社区/讨论语义。"""
    # 左侧主体人像
    p.drawEllipse(QPointF(9, 8), 3.2, 3.2)
    body = QPainterPath()
    body.moveTo(3.2, 20)
    body.cubicTo(3.2, 15.9, 5.8, 13.6, 9, 13.6)
    body.cubicTo(12.2, 13.6, 14.8, 15.9, 14.8, 20)
    p.drawPath(body)
    # 右侧同伴（半身，被轮廓自然裁切）
    p.drawEllipse(QPointF(17, 9.4), 2.4, 2.4)
    mate = QPainterPath()
    mate.moveTo(15.4, 20)
    mate.cubicTo(15.4, 16.8, 17.3, 14.9, 19.6, 14.9)
    mate.cubicTo(20.8, 14.9, 21.8, 15.7, 21.8, 16.9)
    p.drawPath(mate)
    p.drawLine(QPointF(8, 16), QPointF(8.6, 16))


def _model(p: QPainter):
    """模型设置：钥匙（BYOK / API Key 语义）。"""
    # 钥匙环
    p.drawEllipse(QPointF(8, 8), 4.5, 4.5)
    # 钥匙柄（沿 45° 向下伸出）
    p.drawLine(QPointF(11.2, 11.2), QPointF(20.5, 20.5))
    # 两道齿（垂直于柄，朝右上）
    p.drawLine(QPointF(17, 17), QPointF(19, 15))
    p.drawLine(QPointF(19.5, 19.5), QPointF(21.5, 17.5))


def _cost(p: QPainter):
    """AI 用量与成本：坐标轴 + 三根递升柱（统计/费用语义）。"""
    p.drawLine(QPointF(4, 3.5), QPointF(4, 20))
    p.drawLine(QPointF(4, 20), QPointF(21, 20))
    for x, top in ((8, 14), (12.5, 9.5), (17, 5.5)):
        p.drawLine(QPointF(x, 19.2), QPointF(x, top))


def _admin(p: QPainter):
    """管理后台：盾牌 + 对勾（权限/治理语义）。"""
    path = QPainterPath()
    path.moveTo(12, 2.6)
    path.lineTo(20, 5.8)
    path.lineTo(20, 11.6)
    path.cubicTo(20, 16.3, 16.6, 19.9, 12, 21.6)
    path.cubicTo(7.4, 19.9, 4, 16.3, 4, 11.6)
    path.lineTo(4, 5.8)
    path.closeSubpath()
    p.drawPath(path)
    p.drawPolyline([QPointF(8.6, 11.8), QPointF(11.1, 14.3), QPointF(15.4, 9.2)])


def _kb(p: QPainter):
    """本地知识库：数据库圆柱（语料 + 索引的存储语义）。"""
    # 顶面
    p.drawEllipse(QRectF(4.8, 3.4, 14.4, 6.0))
    # 左右立柱
    p.drawLine(QPointF(4.8, 6.4), QPointF(4.8, 17.6))
    p.drawLine(QPointF(19.2, 6.4), QPointF(19.2, 17.6))
    # 中部 / 底部两道下半弧（0 度起、顺时针 180° = 下半圈）
    p.drawArc(QRectF(4.8, 8.2, 14.4, 6.0), 0, -180 * 16)
    p.drawArc(QRectF(4.8, 14.2, 14.4, 6.0), 0, -180 * 16)


def _sparkle(p: QPainter):
    """求职 Agent：四角星（sparkle），与 app_icon 同构，按线性描边绘制。"""
    pts = [
        QPointF(12, 3.75),     # 上尖
        QPointF(14.1, 9.9),    # 右上凹
        QPointF(20.25, 12),    # 右尖
        QPointF(14.1, 14.1),   # 右下凹
        QPointF(12, 20.25),    # 下尖
        QPointF(9.9, 14.1),    # 左下凹
        QPointF(3.75, 12),     # 左尖
        QPointF(9.9, 9.9),     # 左上凹
    ]
    p.drawPolygon(pts)


_DRAWERS = {
    "home": _home,
    "settings": _settings,
    "resume": _resume,
    "search": _search,
    "target": _target,
    "edit": _edit,
    "chat": _chat,
    "banknote": _banknote,
    "clipboard": _clipboard,
    "community": _community,
    "model": _model,
    "cost": _cost,
    "admin": _admin,
    "kb": _kb,
    "sparkle": _sparkle,
}


def _render(drawer, color: str, size: int) -> QPixmap:
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    painter.scale(size / 24.0, size / 24.0)
    pen = QPen(QColor(color))
    pen.setWidthF(_PEN_WIDTH)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    painter.setPen(pen)
    painter.setBrush(Qt.BrushStyle.NoBrush)
    drawer(painter)
    painter.end()
    return pixmap


def nav_icon(name: str, size: int = 18, dark: bool = False) -> QIcon:
    drawer = _DRAWERS[name]
    normal = "#9a9aa3" if dark else _COLOR_NORMAL
    selected = "#a5b4fc" if dark else _COLOR_SELECTED
    icon = QIcon()
    icon.addPixmap(_render(drawer, normal, size), QIcon.Mode.Normal)
    icon.addPixmap(_render(drawer, normal, size), QIcon.Mode.Active)
    icon.addPixmap(_render(drawer, selected, size), QIcon.Mode.Selected)
    return icon


def app_icon(size: int = 64) -> QIcon:
    """应用图标（托盘/窗口用）：深色圆角底 + 白色四角星（sparkle）。"""
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.GlobalColor.transparent)
    p = QPainter(pixmap)
    p.setRenderHint(QPainter.RenderHint.Antialiasing, True)

    s = size / 64.0
    # 深色圆角底
    bg = QPainterPath()
    bg.addRoundedRect(QRectF(2 * s, 2 * s, 60 * s, 60 * s), 17 * s, 17 * s)
    grad = QLinearGradient(0, 0, 0, size)
    grad.setColorAt(0, QColor("#1c1c2e"))
    grad.setColorAt(1, QColor("#0e0e16"))
    p.fillPath(bg, QBrush(grad))

    # 白色四角星（sparkle）
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor("#ffffff"))
    star = [
        QPointF(32 * s, 10 * s),    # 上尖
        QPointF(37.5 * s, 26.5 * s),  # 右上凹
        QPointF(54 * s, 32 * s),    # 右尖
        QPointF(37.5 * s, 37.5 * s),  # 右下凹
        QPointF(32 * s, 54 * s),    # 下尖
        QPointF(26.5 * s, 37.5 * s),  # 左下凹
        QPointF(10 * s, 32 * s),    # 左尖
        QPointF(26.5 * s, 26.5 * s),  # 左上凹
    ]
    p.drawPolygon(star)

    p.end()
    return QIcon(pixmap)
