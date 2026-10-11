#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""离屏渲染投递看板，出图供肉眼核验（浅色 / 深色 / 拖入高亮 三张）。

用途：像素级判据（`test_tracker_kanban.py`）管「颜色对不对」，
这张图管「看起来对不对」—— 列宽、卡片间距、空列提示位置、高亮是否明显。

⚠ 离屏（offscreen）平台下**中文字体可能缺失**，文字会画成方块。
这不是看板的问题，是渲染后端的问题；看布局/配色/间距仍然有效。

运行：python tests/_verify_kanban_shot.py [输出目录]
"""

import os
import sys
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from PySide6.QtCore import QPoint, Qt
from PySide6.QtGui import QDragEnterEvent
from PySide6.QtWidgets import QApplication, QWidget

_APP = QApplication.instance() or QApplication([])

from offerflow_desktop.ui import theme
from offerflow_desktop.ui.pages import tracker

APPS = [
    {"id": "a1", "company": "腾讯", "position": "AI 应用研发", "status": "saved",
     "next_action": "确认岗位是否还开", "next_action_date": "2026-10-12",
     "notes": "", "updated_at": "2026-10-01T10:00:00"},
    {"id": "a2", "company": "合合信息", "position": "RAG 算法", "status": "applied",
     "next_action": "", "next_action_date": "2026-10-08", "notes": "",
     "updated_at": "2026-10-02T10:00:00"},
    {"id": "a3", "company": "幂律智能", "position": "法律科技", "status": "applied",
     "next_action": "09:00 笔试", "next_action_date": "2026-10-09", "notes": "",
     "updated_at": "2026-10-03T10:00:00"},
    {"id": "a4", "company": "字节跳动", "position": "文档智能", "status": "online_test",
     "next_action": "", "next_action_date": None, "notes": "",
     "updated_at": "2026-10-04T10:00:00"},
    {"id": "a5", "company": "美团", "position": "外卖推荐", "status": "first_interview",
     "next_action": "14:30 一面", "next_action_date": "2026-10-11", "notes": "",
     "updated_at": "2026-10-05T10:00:00"},
    {"id": "a6", "company": "百度", "position": "搜索架构", "status": "hr_round",
     "next_action": "等 HR 消息", "next_action_date": "2026-10-13", "notes": "",
     "updated_at": "2026-10-06T10:00:00"},
    {"id": "a7", "company": "京东", "position": "供应链算法", "status": "offered",
     "next_action": "谈薪", "next_action_date": "2026-10-14", "notes": "",
     "updated_at": "2026-10-07T10:00:00"},
    {"id": "a8", "company": "小米", "position": "端侧模型", "status": "rejected",
     "next_action": "", "next_action_date": None, "notes": "",
     "updated_at": "2026-10-08T10:00:00"},
]


class _FakeMW(QWidget):
    def refresh_status(self):
        pass


def render(out_dir: str) -> None:
    os.makedirs(out_dir, exist_ok=True)
    with mock.patch.object(tracker.db, "list_applications",
                           lambda: [dict(a) for a in APPS]), \
            mock.patch.object(tracker, "QMessageBox"):
        page = tracker.TrackerPage(_FakeMW())
        page.resize(1280, 820)

        for name, qss in (("light", theme.MAIN_QSS), ("dark", theme.DARK_QSS)):
            _APP.setStyleSheet(qss)
            page.show()
            _APP.processEvents()
            path = os.path.join(out_dir, "看板-%s.png" % name)
            page.grab().save(path)
            print("已写出", path)

        # 拖入高亮：把「一面」那张卡片拖到「已发 Offer」列上方
        _APP.setStyleSheet(theme.MAIN_QSS)
        _APP.processEvents()
        offered = page._columns[tracker.APPLICATION_STATUSES.index("offered")]
        mime = __import__("PySide6.QtCore", fromlist=["QMimeData"]).QMimeData()
        mime.setData(tracker.KANBAN_MIME, b"a5")
        offered.dragEnterEvent(QDragEnterEvent(
            QPoint(4, 4), Qt.DropAction.MoveAction, mime,
            Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier))
        _APP.processEvents()
        path = os.path.join(out_dir, "看板-拖入高亮.png")
        page.grab().save(path)
        print("已写出", path)


if __name__ == "__main__":
    render(sys.argv[1] if len(sys.argv) > 1 else os.path.join(_ROOT, "_out", "kanban"))
