# -*- coding: utf-8 -*-
"""侧栏截图（浅色 + 深色，普通 + 管理员），用于导航重设计前后对照。

注意：**不能用 QT_QPA_PLATFORM=offscreen** —— 离屏平台的字体库是空的
（``QFontDatabase.families()`` 返回 0 个），中文全部渲染成豆腐块，
截出来的图看不出「副标题会不会被截断」这类真实排版问题。
这里走 Windows 平台插件，窗口会短暂闪现后自动关闭。
"""
import gc
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from PySide6.QtCore import QSize                                        # noqa: E402
from PySide6.QtWidgets import QApplication, QWidget                     # noqa: E402

from offerflow_desktop import api_client as api                         # noqa: E402
from offerflow_desktop import database as db                            # noqa: E402
from offerflow_desktop.ui import theme as theme_mod                     # noqa: E402
from offerflow_desktop.ui.main_window import MainWindow                 # noqa: E402

OUT = os.path.join(ROOT, "_navshots")
os.makedirs(OUT, exist_ok=True)
app = QApplication.instance() or QApplication([])
db.init_db()

tag = sys.argv[1] if len(sys.argv) > 1 else "before"
size = QSize(1360, 920)

for role, role_label in ((False, "user"), (True, "admin")):
    api.is_admin_cached = (lambda r=role: r)
    for dark, dlabel in ((False, "light"), (True, "dark")):
        win = MainWindow()
        app.setStyleSheet(theme_mod.DARK_QSS if dark else theme_mod.MAIN_QSS)
        win.resize(size)
        win.show()
        app.processEvents()
        win.resize(size)
        app.processEvents()

        sidebar = win.findChild(QWidget, "SidebarPanel")
        pm = sidebar.grab()
        path = os.path.join(OUT, "%s_%s_%s.png" % (tag, role_label, dlabel))
        pm.save(path)
        print("saved %s  (%dx%d)" % (path, pm.width(), pm.height()))

        win.hide()
        win.close()
        win.deleteLater()
        app.processEvents()
        gc.collect()
