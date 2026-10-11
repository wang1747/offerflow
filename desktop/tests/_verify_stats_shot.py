#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""离屏渲染「投递统计」页，出图供肉眼核验（浅色 / 深色）。

用途：`test_tracker_stats.py` 管「数字对不对、取色分不分档」，
这张图管「看起来对不对」——条形长度是否成比例、环形占比是否合理、
图例与圆心数字是否压字、空态是否留白恰当。

⚠ 离屏（offscreen）平台下**中文字体可能缺失**，文字会画成方块。
这不是图表的问题，是渲染后端的问题；看布局/配色/比例仍然有效。

运行：python tests/_verify_stats_shot.py [输出目录]
"""

import os
import sys
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from PySide6.QtWidgets import QApplication, QWidget

_APP = QApplication.instance() or QApplication([])

from offerflow_desktop.ui import theme
from offerflow_desktop.ui.pages import tracker

APPS = [
    {"id": "a%d" % i, "company": c, "position": "算法", "status": s,
     "next_action": "", "next_action_date": None, "notes": "",
     "updated_at": "2026-10-0%dT10:00:00" % (i % 9 + 1)}
    for i, (c, s) in enumerate([
        ("腾讯", "saved"), ("阿里", "saved"), ("合合信息", "applied"),
        ("幂律智能", "applied"), ("字节跳动", "applied"), ("百度", "online_test"),
        ("美团", "first_interview"), ("京东", "first_interview"),
        ("小米", "second_interview"), ("快手", "hr_round"),
        ("拼多多", "offered"), ("网易", "accepted"), ("滴滴", "rejected"),
        ("哔哩哔哩", "rejected"),
    ])
]

STATS = {
    "total": len(APPS),
    "status_order": ["saved", "applied", "online_test", "first_interview",
                     "second_interview", "hr_round", "offered", "accepted",
                     "rejected"],
    "status_counts": {"saved": 2, "applied": 3, "online_test": 1,
                      "first_interview": 2, "second_interview": 1,
                      "hr_round": 1, "offered": 1, "accepted": 1, "rejected": 2},
    "conversion_rate": {"applied_to_interview": 42.9, "interview_to_offer": 16.7},
}


class _FakeMW(QWidget):
    def refresh_status(self):
        pass


def render(out_dir: str) -> None:
    os.makedirs(out_dir, exist_ok=True)
    with mock.patch.object(tracker.db, "list_applications",
                           lambda: [dict(a) for a in APPS]), \
            mock.patch.object(tracker.db, "get_statistics",
                              lambda: dict(STATS)), \
            mock.patch.object(tracker, "QMessageBox"):
        page = tracker.TrackerPage(_FakeMW())
        page.resize(1280, 820)

        for name, qss in (("light", theme.MAIN_QSS), ("dark", theme.DARK_QSS)):
            _APP.setStyleSheet(qss)
            tracker.tokens.set_theme(name)
            page.show()
            page.tabs.setCurrentIndex(2)          # 统计 Tab
            _APP.processEvents()
            path = os.path.join(out_dir, "统计-%s.png" % name)
            page.grab().save(path)
            print("已写出", path)

        # 空态：还没有投递记录时长什么样（两条图表都该给占位，不留大白块）
        tracker.tokens.set_theme("light")
        _APP.setStyleSheet(theme.MAIN_QSS)
        empty = {"total": 0, "status_order": list(STATS["status_order"]),
                 "status_counts": {}, "conversion_rate": {}}
        with mock.patch.object(tracker.db, "get_statistics", lambda: dict(empty)):
            page._load_stats()
            _APP.processEvents()
            path = os.path.join(out_dir, "统计-空态.png")
            page.grab().save(path)
            print("已写出", path)


if __name__ == "__main__":
    render(sys.argv[1] if len(sys.argv) > 1 else os.path.join(_ROOT, "_out", "stats"))
