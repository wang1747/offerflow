#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""投递看板「九态分列 + 拖拽改状态」的 UI 层判据。

要钉住的三件事
--------------
1. **拖拽真的改了业务状态**，而不是只把条目从一个 list 挪到另一个 list。
   这是最容易假绿的地方：Qt 自带的拖放会把卡片挪进目标列，界面上「看起来
   成了」，可刷新一次就跳回原列。所以判据必须打**落库的入参** +
   **刷新后新列里真的有它**，两件一起才算数。
2. **不该动的动作一个网络都不碰**：拖回原列、目标状态不在枚举里、
   id 在当前列表里找不到 —— 三种都必须在本地拦下。
3. **空列不能有幽灵卡片**：空列提示若是用一个占位条目实现，
   那一列会「看起来有 1 条」，还会被当成拖拽源。

反向对照（变异测试会逐条回放）：
  M1 去掉「同列 no-op」判断 → 同列拖拽用例变红
  M2 去掉枚举校验 → 非法状态用例变红
  M3 dropEvent 直接交给 Qt 默认实现 → 改状态用例变红
  M4 空列改用占位条目 → 幽灵卡片用例变红

运行：python tests/test_tracker_kanban.py
"""

import os
import sys
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from PySide6.QtCore import QMimeData, QPoint, QPointF, Qt          # noqa: E402
from PySide6.QtGui import QDragEnterEvent, QDragLeaveEvent, QDropEvent  # noqa: E402
from PySide6.QtWidgets import (                                    # noqa: E402
    QApplication, QLabel, QListWidget, QWidget,
)

_APP = QApplication.instance() or QApplication([])

from offerflow_desktop.config import APPLICATION_STATUSES, STATUS_LABELS  # noqa: E402
from offerflow_desktop.ui import theme as tracker_theme                  # noqa: E402
from offerflow_desktop.ui.pages import tracker                            # noqa: E402

APPS = [
    {"id": "a1", "company": "腾讯", "position": "AI 应用研发", "status": "first_interview",
     "next_action": "跟进面试反馈", "next_action_date": "2026-10-12", "notes": "内推",
     "updated_at": "2026-10-01T10:00:00"},
    {"id": "a2", "company": "合合信息", "position": "RAG 算法", "status": "applied",
     "next_action": "", "next_action_date": "2026-10-08", "notes": "",
     "updated_at": "2026-10-02T10:00:00"},
    {"id": "a3", "company": "幂律智能", "position": "法律科技", "status": "applied",
     "next_action": "09:00 笔试", "next_action_date": "2026-10-09", "notes": "",
     "updated_at": "2026-10-03T10:00:00"},
    {"id": "a4", "company": "", "position": "", "status": "saved",
     "next_action": "", "next_action_date": None, "notes": "",
     "updated_at": "2026-10-04T10:00:00"},
    {"id": "a5", "company": "字节跳动", "position": "文档智能", "status": "rejected",
     "next_action": "", "next_action_date": None, "notes": "",
     "updated_at": "2026-10-05T10:00:00"},
]


def _expect_counts():
    counts = {}
    for a in APPS:
        counts[a["status"]] = counts.get(a["status"], 0) + 1
    return counts


EXPECT = _expect_counts()


class _Store:
    """带状态的假后端：改完之后再读，必须读得到改后的样子。"""

    def __init__(self, apps):
        self.apps = [dict(a) for a in apps]
        self.updates = []
        self.reads = 0

    def list(self):
        self.reads += 1
        return [dict(a) for a in self.apps]

    def update(self, app_id, status=None, **kw):
        self.updates.append((app_id, status))
        for a in self.apps:
            if a["id"] == app_id:
                a["status"] = status
        return {"id": app_id, "status": status}


class _FakeMW(QWidget):
    def refresh_status(self):
        pass

    def navigate(self, *a, **k):
        pass

    def open_agent(self, *a, **k):
        pass


def _ids(column) -> list:
    return [column.item(i).data(Qt.ItemDataRole.UserRole) for i in range(column.count())]


def _column(page, status):
    return page._columns[APPLICATION_STATUSES.index(status)]


def _frame(column):
    return column.parentWidget()


def _header_text(column) -> str:
    for lab in _frame(column).findChildren(QLabel):
        if " · " in lab.text():
            return lab.text()
    return ""


def _empty_label(column):
    for lab in _frame(column).findChildren(QLabel):
        if lab.text() == "暂无投递":
            return lab
    return None


def _app_mime(app_id: str) -> QMimeData:
    mime = QMimeData()
    mime.setData(tracker.KANBAN_MIME, str(app_id).encode("utf-8"))
    return mime


class _Base(unittest.TestCase):

    def setUp(self):
        self.store = _Store(APPS)
        self._patches = [
            mock.patch.object(tracker.db, "list_applications", self.store.list),
            mock.patch.object(tracker.db, "update_application", self.store.update),
            mock.patch.object(tracker, "QMessageBox"),
        ]
        for p in self._patches:
            p.start()
            self.addCleanup(p.stop)
        self.mw = _FakeMW()
        self.page = tracker.TrackerPage(self.mw)
        self.addCleanup(self.page.setParent, None)

    def drop(self, column, app_id):
        """把一条投递「拖」到某一列（走真实的 dropEvent 路径）。"""
        mime = _app_mime(app_id)
        event = QDropEvent(QPointF(6, 6), Qt.DropAction.MoveAction, mime,
                           Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
        column.dropEvent(event)
        return event


class TestBoardLayout(_Base):

    def test_nine_columns_in_status_order(self):
        self.assertEqual(len(self.page._columns), len(APPLICATION_STATUSES))
        self.assertEqual([c._status for c in self.page._columns], APPLICATION_STATUSES)

    def test_cards_grouped_by_status(self):
        for status in APPLICATION_STATUSES:
            want = sorted(a["id"] for a in APPS if a["status"] == status)
            self.assertEqual(sorted(_ids(_column(self.page, status))), want,
                             "「%s」列里的卡片不对" % status)

    def test_header_count_matches_cards(self):
        for status in APPLICATION_STATUSES:
            column = _column(self.page, status)
            want = EXPECT.get(status, 0)
            self.assertEqual(_header_text(column),
                             f"{STATUS_LABELS[status]} · {want}")

    def test_reverse_empty_column_has_no_phantom_card(self):
        """空列提示用占位条目实现的话，这一列会「看起来有 1 条」。"""
        column = _column(self.page, "hr_round")
        self.assertEqual(column.count(), 0)
        label = _empty_label(column)
        self.assertIsNotNone(label, "空列少了提示标签")
        self.assertTrue(label.isVisible() or not column.isVisible(),
                        "空列提示应当可见")
        # 对照补：非空列不得显示「暂无投递」
        full = _column(self.page, "applied")
        self.assertGreater(full.count(), 0)
        self.assertFalse(_empty_label(full).isVisible())

    def test_unfilled_company_still_renders(self):
        item = _column(self.page, "saved").item(0)
        self.assertIn("（未填公司）", item.text())

    def test_filter_applies_to_board_too(self):
        idx = self.page.filter_combo.findData("applied")
        self.page.filter_combo.setCurrentIndex(idx)
        self.assertEqual(sorted(_ids(_column(self.page, "applied"))), ["a2", "a3"])
        self.assertEqual(_column(self.page, "saved").count(), 0)


class TestDragAndDrop(_Base):

    def test_setup_knows_how_to_drag(self):
        column = _column(self.page, "applied")
        self.assertTrue(column.dragEnabled())
        self.assertTrue(column.acceptDrops())
        self.assertEqual(column.dragDropMode(),
                         tracker.QAbstractItemView.DragDropMode.DragDrop)

    def test_drag_payload_carries_app_id(self):
        """拖出去时 MIME 里必须真的是这条投递的 id。

        这条不是形式主义：`startDrag` 覆盖了 Qt 默认实现，一旦它没塞数据，
        拖拽会「有动作、无载荷」，落点侧只能静默丢弃 —— 而且不报错。
        """
        column = _column(self.page, "applied")
        column.setCurrentRow(0)
        fake_drag = mock.MagicMock()
        with mock.patch.object(tracker, "QDrag", return_value=fake_drag) as ctor:
            column.startDrag(Qt.DropAction.MoveAction)
        ctor.assert_called_once()
        mime = fake_drag.setMimeData.call_args[0][0]
        self.assertEqual(bytes(mime.data(tracker.KANBAN_MIME)).decode("utf-8"), "a2")
        fake_drag.exec.assert_called_once()

    def test_reverse_drag_without_current_item_sends_nothing(self):
        column = _column(self.page, "applied")
        column.setCurrentRow(-1)
        with mock.patch.object(tracker, "QDrag") as ctor:
            column.startDrag(Qt.DropAction.MoveAction)
        ctor.assert_not_called()

    def test_drop_changes_status_and_persists(self):
        """核心判据：落库入参 + 刷新后新列里真的有它（缺一都是假绿）。"""
        self.drop(_column(self.page, "first_interview"), "a2")
        self.assertEqual(self.store.updates, [("a2", "first_interview")],
                         "状态没真的写到后端")
        self.assertEqual(_ids(_column(self.page, "first_interview")), ["a1", "a2"])
        self.assertEqual(_ids(_column(self.page, "applied")), ["a3"],
                         "刷新后旧列里还留着它 —— 拖拽只挪了视图没改业务")
        self.assertEqual(self.page._apps[
            [a["id"] for a in self.page._apps].index("a2")]["status"],
            "first_interview", "页面缓存的那份列表没跟着更新")
        # 卡片搬到新列之后，表格（同一份 `_apps`）也要跟着对
        row = [a["id"] for a in self.page._apps].index("a2")
        self.assertEqual(self.page.table.item(row, 2).text(), STATUS_LABELS["first_interview"])

    def test_reverse_drop_on_same_column_is_a_noop(self):
        before = self.store.reads
        event = self.drop(_column(self.page, "applied"), "a2")
        self.assertEqual(self.store.updates, [], "拖回原列不该改状态")
        self.assertEqual(self.store.reads, before, "拖回原列不该重读/重刷")
        self.assertTrue(event.isAccepted())

    def test_reverse_unknown_status_is_refused_locally(self):
        """服务端只认九个状态，其它值会 400；本地先写后回滚还得多闪一次警告。"""
        self.assertFalse(self.page._on_kanban_drop("a2", "bogus"))
        self.assertEqual(self.store.updates, [])
        self.assertFalse(tracker.QMessageBox.warning.called)

    def test_reverse_unknown_app_id_is_refused(self):
        self.assertFalse(self.page._on_kanban_drop("不存在", "applied"))
        self.assertEqual(self.store.updates, [])

    def test_reverse_foreign_mime_falls_back_to_qt(self):
        """别的拖放（外部文件、其它列表）不能当成看板改状态。"""
        column = _column(self.page, "rejected")
        mime = QMimeData()
        mime.setText("从别处拖来的东西")
        event = QDropEvent(QPointF(3, 3), Qt.DropAction.MoveAction, mime,
                           Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
        with mock.patch.object(QListWidget, "dropEvent") as base_impl:
            column.dropEvent(event)
        base_impl.assert_called_once()
        self.assertEqual(self.store.updates, [])

    def test_dragover_highlight_is_set_and_cleared(self):
        column = _column(self.page, "offered")
        mime = _app_mime("a2")
        enter = QDragEnterEvent(QPoint(4, 4), Qt.DropAction.MoveAction, mime,
                                Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
        column.dragEnterEvent(enter)
        self.assertEqual(column.property("dragover"), "true")
        self.assertTrue(enter.isAccepted())
        column.dragLeaveEvent(QDragLeaveEvent())
        self.assertEqual(column.property("dragover"), "false")

    def test_reverse_foreign_mime_does_not_highlight(self):
        column = _column(self.page, "offered")
        mime = QMimeData()
        mime.setText("plain text")
        enter = QDragEnterEvent(QPoint(4, 4), Qt.DropAction.MoveAction, mime,
                                Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
        with mock.patch.object(QListWidget, "dragEnterEvent"):
            column.dragEnterEvent(enter)
        self.assertNotEqual(column.property("dragover"), "true")

    def test_failed_write_does_not_move_the_card(self):
        """写失败（比如服务端 4xx）时卡片必须留在原列，不能视觉上先搬走。"""
        def _boom(*a, **k):
            raise tracker.db.ApiError("服务端拒绝")

        with mock.patch.object(tracker.db, "update_application", _boom):
            self.assertFalse(self.page._on_kanban_drop("a2", "offered"))
        self.assertEqual(_ids(_column(self.page, "applied")), ["a2", "a3"])
        self.assertEqual(_ids(_column(self.page, "offered")), [])
        self.assertTrue(tracker.QMessageBox.warning.called, "失败必须告诉用户")


class TestCardActivation(_Base):

    def test_double_click_selects_the_right_row_and_opens_editor(self):
        item = _column(self.page, "rejected").item(0)
        with mock.patch.object(tracker, "EditDialog") as dlg:
            self.page._on_card_activated(item)
        dlg.assert_called_once()
        self.assertEqual(dlg.call_args[0][0]["company"], "字节跳动")
        self.assertEqual(self.page.table.currentRow(),
                         [a["id"] for a in APPS].index("a5"))

    def test_reverse_activation_ignores_cards_without_id(self):
        from PySide6.QtWidgets import QListWidgetItem
        with mock.patch.object(tracker, "EditDialog") as dlg:
            self.page._on_card_activated(QListWidgetItem("无 id"))
        dlg.assert_not_called()


def _lum(hex_color: str) -> float:
    r, g, b = (int(hex_color[i:i + 2], 16) / 255 for i in (1, 3, 5))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


class TestBoardTheme(_Base):
    """看板配色必须**真的画上去**，而不是只写在 QSS 里。

    Qt 对写错的选择器是**静默忽略**的：不报错、不告警，直接退回默认外观。
    所以「源码里有这行 QSS」不能当判据 —— 得离屏把它画出来、按像素比。
    """

    def setUp(self):
        super().setUp()
        self.app = QApplication.instance()
        self.addCleanup(self.app.setStyleSheet, "")
        self.page.resize(1100, 700)
        self.page.show()
        self.addCleanup(self.page.hide)
        self.app.processEvents()
        self.column = _column(self.page, "applied")
        self.frame = _frame(self.column)
        # 取色点：列容器左侧内边距（避开卡片与文字）
        self.probe = (6, self.frame.height() // 2)

    def _pixel(self, pos):
        img = self.frame.grab().toImage()
        x = max(0, min(pos[0], img.width() - 1))
        y = max(0, min(pos[1], img.height() - 1))
        c = img.pixelColor(x, y)
        return "#%02x%02x%02x" % (c.red(), c.green(), c.blue())

    def _to_frame(self, pos):
        """viewport 坐标 → frame 坐标。

        ⚠ 两个坐标系不能混用：条目矩形是**相对 viewport** 的，而取色是在
        **frame** 上 grab 的。直接拿 viewport 的 y 当 frame 的 y 用，会整体
        偏掉一个 viewport 偏移（这里恰好是一张卡片的高度），于是「取列表空白」
        变成「取卡片本身」，判据看着在跑、其实什么都没测到。
        """
        p = self.column.viewport().mapTo(self.frame, pos)
        return (p.x(), p.y())

    def _card_pixel(self):
        rect = self.column.visualItemRect(self.column.item(0))
        self.assertTrue(rect.isValid(), "卡片没有几何信息，取色判据成了空靶")
        return self._pixel(self._to_frame(QPoint(rect.center().x() + rect.width() // 3,
                                                rect.center().y())))

    def _list_probe(self):
        """列表内部、最后一张卡片下方的空白点（用来验列表自己那层背景）。"""
        last = self.column.visualItemRect(self.column.item(self.column.count() - 1))
        self.assertTrue(last.isValid(), "卡片没有几何信息，取色判据成了空靶")
        return self._to_frame(QPoint(last.center().x(), last.bottom() + 20))

    def _snapshot(self, qss: str) -> dict:
        self.app.setStyleSheet(qss)
        self.app.processEvents()
        return {"frame": self._pixel(self.probe),
                "card": self._card_pixel(),
                "list": self._pixel(self._list_probe())}

    def test_column_and_card_colors_follow_the_theme(self):
        light = self._snapshot(tracker_theme.MAIN_QSS)
        dark = self._snapshot(tracker_theme.DARK_QSS)
        bare = self._snapshot("")
        # 不挂样式表时是系统默认底色：与两套主题都不同 ⇒ 颜色确实来自我们的 QSS，
        # 而不是某个本来就有的默认值（这一条才是判据不空的保证）
        self.assertNotEqual(bare["frame"], light["frame"])
        self.assertNotEqual(bare["frame"], dark["frame"])
        self.assertGreater(_lum(light["frame"]), 0.85, "浅色主题下列容器应当是浅底")
        self.assertLess(_lum(dark["frame"]), 0.25, "深色主题下列容器应当是深底")
        # 卡片必须与列底分开，否则一张张卡片在列里糊成一片
        self.assertNotEqual(light["card"], light["frame"])
        self.assertNotEqual(dark["card"], dark["frame"])
        # 列表自己在列底之上必须是**透明**的：它若自己刷一层底，列容器的配色
        # 就白做了，而且会多出一圈边框（`QListWidget` 的全局规则带 border）。
        self.assertEqual(light["list"], light["frame"],
                         "列表没透明 —— 列底配色被盖住了")
        self.assertEqual(dark["list"], dark["frame"],
                         "列表没透明 —— 列底配色被盖住了")

    def test_dragover_highlight_shows_and_clears(self):
        self.app.setStyleSheet(tracker_theme.MAIN_QSS)
        self.app.processEvents()
        column = self.column
        # 落点高亮画在列表内部（列底之上的半透明色），所以要取**最后一张卡片
        # 下方**的列表空白 —— 取第一张后面会落到第二张卡片上，等于什么都没测。
        probe = self._list_probe()
        # 先自证「取到的是列底而不是卡片」，否则下面那个 assertNotEqual 可能
        # 只是把白卡片换成另一种白
        self.assertEqual(self._pixel(probe), self._pixel(self.probe),
                         "取色点没落在列表空白上")

        before = self._pixel(probe)
        mime = _app_mime("a2")
        column.dragEnterEvent(QDragEnterEvent(
            QPoint(4, 4), Qt.DropAction.MoveAction, mime,
            Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier))
        self.assertNotEqual(self._pixel(probe), before, "拖入时看不出落点")
        column.dragLeaveEvent(QDragLeaveEvent())
        self.assertEqual(self._pixel(probe), before, "拖走后高亮没清掉")


if __name__ == "__main__":
    unittest.main(verbosity=2)
