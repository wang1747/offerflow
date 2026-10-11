"""工作台 QSS 主题（克制专业风：白色侧栏 + 单一主色 + 干净卡片）。

参考 Notion / Linear / 语雀等专业工作台：去渐变、去多余色彩，
用清晰的信息层级、统一间距和单一强调色营造专业感。
"""

MAIN_QSS = """
* { font-family: "Microsoft YaHei", "微软雅黑", sans-serif; }

QMainWindow, QWidget { background: #f7f8fa; color: #1a1a2e; }

/* ============================================================
   左侧导航（品牌区 + 两行条目 + 底部用户卡）
   ============================================================ */
#SidebarPanel { background: #ffffff; border-right: 1px solid #ececf1; }

/* 品牌区 */
#NavBrand { background: transparent; }
#NavBrandMark {
    font-size: 13px; font-weight: 800; color: #ffffff;
    background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
                                stop:0 #6366f1, stop:1 #4f46e5);
    border-radius: 9px;
}
#NavBrandName { font-size: 15px; font-weight: 700; color: #1a1a2e; }
#NavBrandSub { font-size: 10px; color: #9ca3af; }

/* 滚动容器：自己画 6px 细滚动条，默认主题的 10px 在侧栏里太抢眼 */
#NavScroll, #NavBody { background: transparent; border: none; }
#NavScroll QScrollBar:vertical { background: transparent; width: 6px; margin: 2px 0; }
#NavScroll QScrollBar::handle:vertical {
    background: #d8dbe3; border-radius: 3px; min-height: 24px;
}
#NavScroll QScrollBar::handle:vertical:hover { background: #bcc0cb; }
#NavScroll QScrollBar::add-line:vertical, #NavScroll QScrollBar::sub-line:vertical { height: 0; }

/* 分组标题：不是导航项（不可点、不高亮），所以不叫 NavItem */
#NavGroupLabel { font-size: 10px; font-weight: 700; color: #9ca3af; padding: 12px 12px 2px 12px; }
#NavGroupLabel[first="true"] { padding-top: 2px; }

/* 导航条目 */
#NavRow { background: transparent; border: 1px solid transparent; border-radius: 8px; }
#NavRow:hover { background: #f3f4f8; }
#NavRow[active="true"] { background: #eef0fe; }
/* focus 只换描边颜色、不新增边框：否则键盘走到哪一行，那一行内容会平移 1px */
#NavRow:focus { border-color: #c7cbf5; }
#NavIcon { background: transparent; }
#NavIndicator { background: transparent; border-radius: 2px; }
#NavIndicator[active="true"] { background: #4f46e5; }
#NavRowTitle { font-size: 13px; font-weight: 500; color: #4a4f5c; background: transparent; }
#NavRowTitle[active="true"] { color: #4338ca; font-weight: 700; }
#NavRowSub { font-size: 10px; color: #9ca3af; background: transparent; }
#NavRowSub[active="true"] { color: #7379d4; }

/* 底部用户卡 */
#NavSeparator { background: #ececf1; border: none; }
#NavUserCard { background: transparent; border: none; }
#NavUserCard:hover { background: #f7f8fa; }
#NavAvatar {
    font-size: 13px; font-weight: 700; color: #4f46e5;
    background: #eef0fe; border-radius: 16px;
}
#NavUserName { font-size: 12px; font-weight: 600; color: #1a1a2e; background: transparent; }
#NavUserRole { font-size: 10px; color: #9ca3af; background: transparent; }
QPushButton[class="navicon"] {
    background: transparent; border: 1px solid #e8eaf0; border-radius: 6px;
    color: #6b7280; font-size: 11px; padding: 0;
}
QPushButton[class="navicon"]:hover {
    background: #fdf3f3; border-color: #f3d7d7; color: #dc2626;
}
#SidebarVersion { font-size: 10px; color: #b3b8c4; padding: 2px 14px 10px 14px; }

/* ============================================================
   登录窗口
   ============================================================ */
#LoginLogo { font-size: 30px; font-weight: 800; color: #4f46e5; }
#LoginSub { font-size: 13px; color: #6b7280; }
#LoginSwitchHint { font-size: 13px; color: #6b7280; }

/* ============================================================
   内容区
   ============================================================ */
#PageTitle { font-size: 20px; font-weight: 700; color: #1a1a2e; }
#PageDesc { font-size: 12px; color: #6b7280; }

QLabel { color: #1a1a2e; background: transparent; }
/* 纯布局容器：不加这条会吃到全局 `QMainWindow, QWidget` 的页面底色
   （#f7f8fa），放进白色卡片里就是一块灰。重写了 paintEvent 的自绘控件
   （charts.py 那几个）不受影响 —— 它们本来就不画 QSS 背景。 */
QWidget[class="flush"] { background: transparent; }
QLabel[class="hint"] { color: #6b7280; font-size: 12px; }
QLabel[class="muted"] { color: #9ca3af; font-size: 12px; }
QLabel[class="score"] { font-size: 26px; font-weight: 800; }
QLabel[class="section-title"] { font-size: 14px; font-weight: 700; color: #1a1a2e; }

/* 评分区间色（原为内联 `color: #059669/#d97706/#dc2626`，深色下仅 2.67:1 不可读） */
QLabel[class="score-good"] { font-size: 26px; font-weight: 800; color: #15803d; }
QLabel[class="score-mid"] { font-size: 26px; font-weight: 800; color: #b45309; }
QLabel[class="score-bad"] { font-size: 26px; font-weight: 800; color: #dc2626; }

/* ---- 统一视觉规范：语义文字色 ----
   页面**不要**再写内联 `color: #xxx`（内联优先级高于 QSS，会把深色主题写死成浅色）。
   下列色值均按 WCAG AA 在各自主题底色上校准（正文 ≥ 4.5:1）。 */
*[class="text-brand"] { color: #4338ca; }
*[class="text-success"] { color: #15803d; }
*[class="text-warn"] { color: #b45309; }
*[class="text-danger"] { color: #dc2626; }
*[class="text-info"] { color: #0369a1; }

/* ---- 统一视觉规范：字号档位 ---- */
*[class="strong"] { font-size: 13px; font-weight: 700; }
*[class="card-title"] { font-size: 15px; font-weight: 700; }
*[class="num"] { font-size: 20px; font-weight: 800; }
*[class="num-lg"] { font-size: 22px; font-weight: 800; }
*[class="num-sm"] { font-size: 16px; font-weight: 700; }
*[class="mono"] { font-family: Consolas, "Courier New", monospace; font-size: 12px; }
*[class="tiny"] { font-size: 11px; color: #9ca3af; }

/* 嵌套小卡（卡片内的浅底分组块） */
*[class="subcard"] {
    border: 1px solid #e8eaf0; border-radius: 8px; padding: 8px 10px;
}
*[class="subcard-warn"] {
    border: 1px solid #f0e4c8; background: #fffbeb; border-radius: 8px; padding: 8px 10px;
}

/* ============================================================
   卡片
   ============================================================ */
QFrame[class="card"] {
    background: #ffffff; border: 1px solid #e8eaf0;
    border-radius: 8px;
}
QFrame[class="card-highlight"] {
    background: #ffffff; border: 1px solid #e0e2ec; border-radius: 8px;
}
QFrame[class="card-success"] {
    background: #ffffff; border: 1px solid #dcefe6; border-radius: 8px;
}
QFrame[class="card-warn"] {
    background: #ffffff; border: 1px solid #f0e4c8; border-radius: 8px;
}
QFrame[class="card-danger"] {
    background: #ffffff; border: 1px solid #f3d7d7; border-radius: 8px;
}
/* 成本页的可展开调用记录行（点开看链路明细） */
QFrame#TraceRow {
    background: #fbfbfd; border: 1px solid #eef0f5; border-radius: 8px;
}
QFrame#TraceRow:hover { border-color: #d6d9e0; }
QFrame#TraceRow[open="true"] { background: #f7f7ff; border-color: #dcdcf5; }

/* ============================================================
   按钮
   ============================================================ */
QPushButton {
    background: #ffffff; border: 1px solid #d6d9e0; border-radius: 6px;
    padding: 7px 16px; color: #3a3f4a; font-size: 13px;
}
QPushButton:hover { border-color: #4f46e5; color: #4f46e5; background: #ffffff; }
QPushButton:pressed { background: #f5f6fa; }
QPushButton:disabled { color: #c3c7d0; border-color: #e5e7ee; background: #fafafa; }

QPushButton[class="primary"] {
    background: #4f46e5; border: none; color: #ffffff; font-weight: 600; padding: 8px 20px;
}
QPushButton[class="primary"]:hover { background: #4338ca; }
QPushButton[class="primary"]:disabled { background: #b7b4ee; }

QPushButton[class="success"] {
    background: #16a34a; border: none; color: #ffffff; font-weight: 600; padding: 8px 20px;
}
QPushButton[class="success"]:hover { background: #15803d; }

QPushButton[class="danger"] {
    background: #ffffff; border: 1px solid #f0b4b4; color: #dc2626;
}
QPushButton[class="danger"]:hover { background: #fdf3f3; border-color: #ef4444; }

QPushButton[class="ghost"] {
    background: #f1f2f6; border: 1px solid #e5e7ee; color: #3a3f4a;
}
QPushButton[class="ghost"]:hover { background: #e8eaf0; }

/* 会话列表行（Agent 左栏历史会话）：左对齐、两行文本（上下文 + 时间） */
QPushButton[class="session-item"] {
    background: #ffffff; border: 1px solid #e5e7ee; border-radius: 6px;
    color: #3a3f4a; text-align: left; padding: 6px 10px; font-size: 12px;
}
QPushButton[class="session-item"]:hover { background: #f5f6fa; border-color: #d6d9e0; }
QPushButton[class="session-item"][active="true"] {
    background: #eef0fe; border-color: #c7cbf7; color: #4f46e5; font-weight: 600;
}

/* 分类筛选 chip（可选中）：未选 = 灰底描边，选中 = 主色实心 */
QPushButton[class="chip"] {
    background: #f1f2f6; border: 1px solid #e5e7ee; color: #5f6470;
    padding: 6px 14px; border-radius: 15px; font-size: 12px;
}
QPushButton[class="chip"]:hover { background: #e8eaf0; border-color: #d6d9e0; color: #3a3f4a; }
QPushButton[class="chip"]:checked {
    background: #4f46e5; border: 1px solid #4f46e5; color: #ffffff; font-weight: 600;
}
QPushButton[class="chip"]:checked:hover { background: #4338ca; border-color: #4338ca; color: #ffffff; }

/* ============================================================
   输入控件
   ============================================================ */
QLineEdit, QTextEdit, QPlainTextEdit, QComboBox, QSpinBox, QDateEdit {
    background: #ffffff; border: 1px solid #d6d9e0; border-radius: 6px;
    padding: 6px 10px; font-size: 13px; selection-background-color: #4f46e5;
}
QLineEdit:focus, QTextEdit:focus, QPlainTextEdit:focus, QComboBox:focus, QSpinBox:focus {
    border: 1px solid #4f46e5; background: #ffffff;
}
QComboBox::drop-down { border: none; width: 26px; }
QComboBox::down-arrow {
    image: none; border-left: 4px solid transparent; border-right: 4px solid transparent;
    border-top: 5px solid #9ca3af; margin-right: 8px;
}
QComboBox QAbstractItemView {
    background: #ffffff; border: 1px solid #e8eaf0; border-radius: 6px;
    selection-background-color: #eef0fe; selection-color: #4f46e5; padding: 4px;
}

/* ============================================================
   列表 / 表格
   ============================================================ */
QListWidget, QListView, QTableWidget, QTableView {
    background: #ffffff; border: 1px solid #e8eaf0; border-radius: 8px;
    font-size: 13px;
}
QListWidget::item { padding: 9px 10px; border-radius: 6px; }
QListWidget::item:hover { background: #f5f6fa; }
QListWidget::item:selected { background: #eef0fe; color: #4f46e5; }
/* 投递看板：一列一个「列容器」，卡片列表本身透明、只当落点 */
QFrame[class="kanban-col"] {
    background: #f8fafc; border: 1px solid #e8eaf0; border-radius: 10px;
}
QListWidget[class="kanban-list"] { background: transparent; border: none; }
QListWidget[class="kanban-list"]::item {
    background: #ffffff; border: 1px solid #e2e8f0; border-radius: 6px;
    padding: 6px 8px; color: #1f2937;
}
QListWidget[class="kanban-list"]::item:hover {
    background: #f5f6fa; border-color: #c7d2fe;
}
QListWidget[class="kanban-list"]::item:selected {
    background: #eef0fe; border-color: #4f46e5; color: #1f2937;
}
QListWidget[class="kanban-list"][dragover="true"] {
    background: rgba(79, 70, 229, 0.10); border-radius: 8px;
}
QTableWidget { gridline-color: #f1f2f6; }
QTableWidget::item { padding: 4px; }
QHeaderView::section {
    background: #fafafa; border: none; border-bottom: 1px solid #e8eaf0;
    padding: 9px 8px; font-weight: 600; color: #5f6470;
}

/* ============================================================
   标签页
   ============================================================ */
QTabWidget::pane { border: 1px solid #e8eaf0; border-radius: 8px; background: #ffffff; }
QTabBar::tab {
    background: transparent; color: #6b7280; padding: 9px 20px;
    border-bottom: 2px solid transparent; margin-right: 2px; font-size: 13px;
}
QTabBar::tab:hover { color: #4f46e5; }
QTabBar::tab:selected {
    color: #4f46e5; font-weight: 600; border-bottom: 2px solid #4f46e5;
    background: transparent;
}

/* ============================================================
   进度条
   ============================================================ */
QProgressBar {
    background: #e8eaf0; border: none; border-radius: 5px; height: 10px;
    text-align: center; color: transparent;
}
QProgressBar::chunk { background: #4f46e5; border-radius: 5px; }

/* ============================================================
   滚动条
   ============================================================ */
QScrollArea { border: none; background: transparent; }
QScrollBar:vertical { background: transparent; width: 10px; margin: 2px; }
QScrollBar::handle:vertical { background: #d0d3dc; border-radius: 5px; min-height: 30px; }
QScrollBar::handle:vertical:hover { background: #b3b7c2; }
QScrollBar::add-line, QScrollBar::sub-line { height: 0; }
QScrollBar:horizontal { background: transparent; height: 10px; margin: 2px; }
QScrollBar::handle:horizontal { background: #d0d3dc; border-radius: 5px; min-width: 30px; }
QScrollBar::handle:horizontal:hover { background: #b3b7c2; }

/* ============================================================
   离线提示条
   断网/服务器故障时显示本地副本提示。配色按 WCAG AA 校准：
   #8c5a00 文字 on #fff7e6 背景 ≈ 5.5:1（正文要求 ≥ 4.5:1）。
   ============================================================ */
#OfflineBanner {
    background: #fff7e6;
    border-bottom: 1px solid #ffd591;
}
#OfflineBannerText { color: #8c5a00; font-size: 12px; }

/* ============================================================
   状态栏 / 分组框 / 杂项
   ============================================================ */
QStatusBar { background: #ffffff; border-top: 1px solid #ececf1; color: #6b7280; font-size: 12px; }
QGroupBox {
    border: 1px solid #e8eaf0; border-radius: 8px; margin-top: 12px;
    font-size: 13px; font-weight: 700; color: #3a3f4a; background: #ffffff;
}
QGroupBox::title { subcontrol-origin: margin; left: 12px; padding: 0 6px; }
QSplitter::handle { background: #e8eaf0; width: 1px; }
QToolTip {
    background: #1a1a2e; color: #ffffff; border: none;
    padding: 7px 10px; border-radius: 6px; font-size: 12px;
}
QCheckBox { spacing: 7px; }
QCheckBox::indicator {
    width: 16px; height: 16px; border: 1px solid #c3c7d0; border-radius: 4px; background: #ffffff;
}
QCheckBox::indicator:checked { background: #4f46e5; border: 1px solid #4f46e5; }
QMenu { background: #ffffff; border: 1px solid #e8eaf0; border-radius: 8px; padding: 4px; }
QMenu::item { padding: 6px 20px; border-radius: 6px; }
QMenu::item:selected { background: #eef0fe; color: #4f46e5; }
"""


DARK_QSS = """
* { font-family: "Microsoft YaHei", "微软雅黑", sans-serif; }

QMainWindow, QWidget { background: #17171c; color: #e4e4e7; }

/* ============================================================
   左侧导航（品牌区 + 两行条目 + 底部用户卡）
   ============================================================ */
#SidebarPanel { background: #1e1e26; border-right: 1px solid #2a2a35; }

/* 品牌区 */
#NavBrand { background: transparent; }
#NavBrandMark {
    font-size: 13px; font-weight: 800; color: #ffffff;
    background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
                                stop:0 #6366f1, stop:1 #4f46e5);
    border-radius: 9px;
}
#NavBrandName { font-size: 15px; font-weight: 700; color: #f4f4f5; }
#NavBrandSub { font-size: 10px; color: #6b6b74; }

/* 滚动容器 */
#NavScroll, #NavBody { background: transparent; border: none; }
#NavScroll QScrollBar:vertical { background: transparent; width: 6px; margin: 2px 0; }
#NavScroll QScrollBar::handle:vertical {
    background: #3a3a46; border-radius: 3px; min-height: 24px;
}
#NavScroll QScrollBar::handle:vertical:hover { background: #4d4d5c; }
#NavScroll QScrollBar::add-line:vertical, #NavScroll QScrollBar::sub-line:vertical { height: 0; }

/* 分组标题 */
#NavGroupLabel { font-size: 10px; font-weight: 700; color: #6b6b74; padding: 12px 12px 2px 12px; }
#NavGroupLabel[first="true"] { padding-top: 2px; }

/* 导航条目 */
#NavRow { background: transparent; border: 1px solid transparent; border-radius: 8px; }
#NavRow:hover { background: #26262f; }
#NavRow[active="true"] { background: #2c2c48; }
#NavRow:focus { border-color: #3f3f63; }
#NavIcon { background: transparent; }
#NavIndicator { background: transparent; border-radius: 2px; }
#NavIndicator[active="true"] { background: #818cf8; }
#NavRowTitle { font-size: 13px; font-weight: 500; color: #9a9aa3; background: transparent; }
#NavRowTitle[active="true"] { color: #c7cbf7; font-weight: 700; }
#NavRowSub { font-size: 10px; color: #63636c; background: transparent; }
#NavRowSub[active="true"] { color: #8b8fd8; }

/* 底部用户卡 */
#NavSeparator { background: #2a2a35; border: none; }
#NavUserCard { background: transparent; border: none; }
#NavUserCard:hover { background: #26262f; }
#NavAvatar {
    font-size: 13px; font-weight: 700; color: #c7cbf7;
    background: #2c2c48; border-radius: 16px;
}
#NavUserName { font-size: 12px; font-weight: 600; color: #e4e4e7; background: transparent; }
#NavUserRole { font-size: 10px; color: #6b6b74; background: transparent; }
QPushButton[class="navicon"] {
    background: transparent; border: 1px solid #2e2e38; border-radius: 6px;
    color: #9a9aa3; font-size: 11px; padding: 0;
}
QPushButton[class="navicon"]:hover {
    background: #3a2727; border-color: #4d3232; color: #f87171;
}
#SidebarVersion { font-size: 10px; color: #4f4f58; padding: 2px 14px 10px 14px; }

/* ============================================================
   登录窗口
   ============================================================ */
#LoginLogo { font-size: 30px; font-weight: 800; color: #818cf8; }
#LoginSub { font-size: 13px; color: #9a9aa3; }
#LoginSwitchHint { font-size: 13px; color: #9a9aa3; }

/* ============================================================
   内容区
   ============================================================ */
#PageTitle { font-size: 20px; font-weight: 700; color: #f4f4f5; }
#PageDesc { font-size: 12px; color: #9a9aa3; }

QLabel { color: #e4e4e7; background: transparent; }
/* 纯布局容器：见浅色主题同名规则 */
QWidget[class="flush"] { background: transparent; }
QLabel[class="hint"] { color: #9a9aa3; font-size: 12px; }
QLabel[class="muted"] { color: #6b6b74; font-size: 12px; }
QLabel[class="score"] { font-size: 26px; font-weight: 800; }
QLabel[class="section-title"] { font-size: 14px; font-weight: 700; color: #f4f4f5; }

/* 评分区间色（深色下提亮，保证在深底上 ≥ 4.5:1） */
QLabel[class="score-good"] { font-size: 26px; font-weight: 800; color: #4ade80; }
QLabel[class="score-mid"] { font-size: 26px; font-weight: 800; color: #fbbf24; }
QLabel[class="score-bad"] { font-size: 26px; font-weight: 800; color: #f87171; }

/* ---- 统一视觉规范：语义文字色（深色下取亮色，保证在深底上 ≥ 4.5:1）---- */
*[class="text-brand"] { color: #a5b4fc; }
*[class="text-success"] { color: #4ade80; }
*[class="text-warn"] { color: #fbbf24; }
*[class="text-danger"] { color: #f87171; }
*[class="text-info"] { color: #38bdf8; }

/* ---- 统一视觉规范：字号档位 ---- */
*[class="strong"] { font-size: 13px; font-weight: 700; }
*[class="card-title"] { font-size: 15px; font-weight: 700; }
*[class="num"] { font-size: 20px; font-weight: 800; }
*[class="num-lg"] { font-size: 22px; font-weight: 800; }
*[class="num-sm"] { font-size: 16px; font-weight: 700; }
*[class="mono"] { font-family: Consolas, "Courier New", monospace; font-size: 12px; }
*[class="tiny"] { font-size: 11px; color: #6b6b74; }

/* 嵌套小卡 */
*[class="subcard"] {
    border: 1px solid #2e2e38; border-radius: 8px; padding: 8px 10px;
}
*[class="subcard-warn"] {
    border: 1px solid #3d3423; background: #2b2416; border-radius: 8px; padding: 8px 10px;
}

/* ============================================================
   卡片
   ============================================================ */
QFrame[class="card"] { background: #22222a; border: 1px solid #2e2e38; border-radius: 8px; }
QFrame[class="card-highlight"] { background: #22222a; border: 1px solid #34344a; border-radius: 8px; }
QFrame[class="card-success"] { background: #22222a; border: 1px solid #1f3a2e; border-radius: 8px; }
QFrame[class="card-warn"] { background: #22222a; border: 1px solid #3d3423; border-radius: 8px; }
QFrame[class="card-danger"] { background: #22222a; border: 1px solid #3d2727; border-radius: 8px; }
/* 成本页的可展开调用记录行（点开看链路明细） */
QFrame#TraceRow { background: #26262f; border: 1px solid #33333f; border-radius: 8px; }
QFrame#TraceRow:hover { border-color: #3f3f4d; }
QFrame#TraceRow[open="true"] { background: #2a2a3a; border-color: #3f3f5e; }

/* ============================================================
   按钮
   ============================================================ */
QPushButton {
    background: #2a2a33; border: 1px solid #3a3a45; border-radius: 6px;
    padding: 7px 16px; color: #d4d4d8; font-size: 13px;
}
QPushButton:hover { border-color: #6366f1; color: #a5b4fc; }
QPushButton:pressed { background: #31313c; }
QPushButton:disabled { color: #55555e; border-color: #2e2e38; background: #23232b; }

QPushButton[class="primary"] {
    background: #6366f1; border: none; color: #ffffff; font-weight: 600; padding: 8px 20px;
}
QPushButton[class="primary"]:hover { background: #4f46e5; }
QPushButton[class="primary"]:disabled { background: #3a3a55; }

QPushButton[class="success"] {
    background: #16a34a; border: none; color: #ffffff; font-weight: 600; padding: 8px 20px;
}
QPushButton[class="success"]:hover { background: #15803d; }

QPushButton[class="danger"] {
    background: #2a2a33; border: 1px solid #7f3b3b; color: #fca5a5;
}
QPushButton[class="danger"]:hover { background: #3a2525; border-color: #ef4444; }

/* 分类筛选 chip（可选中）：未选 = 深灰描边，选中 = 主色实心 */
QPushButton[class="chip"] {
    background: #2a2a33; border: 1px solid #3a3a45; color: #a1a1aa;
    padding: 6px 14px; border-radius: 15px; font-size: 12px;
}
QPushButton[class="chip"]:hover { background: #31313c; border-color: #4a4a57; color: #d4d4d8; }
QPushButton[class="chip"]:checked {
    background: #6366f1; border: 1px solid #6366f1; color: #ffffff; font-weight: 600;
}
QPushButton[class="chip"]:checked:hover { background: #4f46e5; border-color: #4f46e5; color: #ffffff; }

/* 会话列表行（Agent 左栏历史会话）：左对齐、两行文本（上下文 + 时间） */
QPushButton[class="session-item"] {
    background: #22222a; border: 1px solid #2e2e38; border-radius: 6px;
    color: #d4d4d8; text-align: left; padding: 6px 10px; font-size: 12px;
}
QPushButton[class="session-item"]:hover { background: #2a2a33; border-color: #3a3a45; }
QPushButton[class="session-item"][active="true"] {
    background: #2c2c48; border-color: #4a4a6a; color: #a5b4fc; font-weight: 600;
}

/* ============================================================
   输入控件
   ============================================================ */
QLineEdit, QTextEdit, QPlainTextEdit, QComboBox, QSpinBox, QDateEdit {
    background: #1e1e26; border: 1px solid #3a3a45; border-radius: 6px;
    padding: 6px 10px; font-size: 13px; color: #e4e4e7; selection-background-color: #6366f1;
}
QLineEdit:focus, QTextEdit:focus, QPlainTextEdit:focus, QComboBox:focus, QSpinBox:focus {
    border: 1px solid #6366f1;
}
QComboBox::drop-down { border: none; width: 26px; }
QComboBox QAbstractItemView {
    background: #22222a; border: 1px solid #2e2e38; border-radius: 6px;
    selection-background-color: #2c2c48; selection-color: #a5b4fc; padding: 4px;
}

/* ============================================================
   列表 / 表格
   ============================================================ */
QListWidget, QListView, QTableWidget, QTableView {
    background: #1e1e26; border: 1px solid #2e2e38; border-radius: 8px;
    font-size: 13px; color: #e4e4e7;
}
QListWidget::item { padding: 9px 10px; border-radius: 6px; }
QListWidget::item:hover { background: #26262f; }
QListWidget::item:selected { background: #2c2c48; color: #a5b4fc; }
/* 投递看板：一列一个「列容器」，卡片列表本身透明、只当落点 */
QFrame[class="kanban-col"] {
    background: #1a1a21; border: 1px solid #2e2e38; border-radius: 10px;
}
QListWidget[class="kanban-list"] { background: transparent; border: none; }
QListWidget[class="kanban-list"]::item {
    background: #26262f; border: 1px solid #34343f; border-radius: 6px;
    padding: 6px 8px; color: #e4e4e7;
}
QListWidget[class="kanban-list"]::item:hover {
    background: #2f2f3a; border-color: #4f46e5;
}
QListWidget[class="kanban-list"]::item:selected {
    background: #2c2c48; border-color: #818cf8; color: #e4e4e7;
}
QListWidget[class="kanban-list"][dragover="true"] {
    background: rgba(129, 140, 248, 0.16); border-radius: 8px;
}
QTableWidget { gridline-color: #2a2a35; }
QHeaderView::section {
    background: #1e1e26; border: none; border-bottom: 1px solid #2e2e38;
    padding: 9px 8px; font-weight: 600; color: #9a9aa3;
}

/* ============================================================
   标签页
   ============================================================ */
QTabWidget::pane { border: 1px solid #2e2e38; border-radius: 8px; background: #22222a; }
QTabBar::tab {
    background: transparent; color: #9a9aa3; padding: 9px 20px;
    border-bottom: 2px solid transparent; margin-right: 2px; font-size: 13px;
}
QTabBar::tab:hover { color: #a5b4fc; }
QTabBar::tab:selected { color: #a5b4fc; font-weight: 600; border-bottom: 2px solid #6366f1; }

/* ============================================================
   进度条 / 滚动条
   ============================================================ */
QProgressBar {
    background: #2e2e38; border: none; border-radius: 5px; height: 10px;
    text-align: center; color: transparent;
}
QProgressBar::chunk { background: #6366f1; border-radius: 5px; }

QScrollArea { border: none; background: transparent; }
QScrollBar:vertical { background: transparent; width: 10px; margin: 2px; }
QScrollBar::handle:vertical { background: #3a3a45; border-radius: 5px; min-height: 30px; }
QScrollBar::handle:vertical:hover { background: #4a4a55; }
QScrollBar::add-line, QScrollBar::sub-line { height: 0; }
QScrollBar:horizontal { background: transparent; height: 10px; margin: 2px; }
QScrollBar::handle:horizontal { background: #3a3a45; border-radius: 5px; min-width: 30px; }
QScrollBar::handle:horizontal:hover { background: #4a4a55; }

/* ============================================================
   状态栏 / 分组框 / 杂项
   ============================================================ */
QStatusBar { background: #1e1e26; border-top: 1px solid #2a2a35; color: #9a9aa3; font-size: 12px; }
QGroupBox {
    border: 1px solid #2e2e38; border-radius: 8px; margin-top: 12px;
    font-size: 13px; font-weight: 700; color: #d4d4d8; background: #22222a;
}
QGroupBox::title { subcontrol-origin: margin; left: 12px; padding: 0 6px; }
QSplitter::handle { background: #2e2e38; width: 1px; }
QToolTip { background: #33333f; color: #f4f4f5; border: none; padding: 7px 10px; border-radius: 6px; font-size: 12px; }
QCheckBox { spacing: 7px; }
QCheckBox::indicator {
    width: 16px; height: 16px; border: 1px solid #4a4a55; border-radius: 4px; background: #1e1e26;
}
QCheckBox::indicator:checked { background: #6366f1; border: 1px solid #6366f1; }
QMenu { background: #22222a; border: 1px solid #2e2e38; border-radius: 8px; padding: 4px; }
QMenu::item { padding: 6px 20px; border-radius: 6px; color: #e4e4e7; }
QMenu::item:selected { background: #2c2c48; color: #a5b4fc; }
"""


SYSTEM_THEME = "system"


def system_theme() -> str:
    """当前操作系统的明暗设置，返回 ``"dark"`` 或 ``"light"``。

    两条路，先问 Qt、再问注册表：

      * ``QStyleHints.colorScheme()`` 是 Qt 6.5+ 的标准做法，而且**会随系统
        变化发信号**（见 ``MainWindow._watch_system_theme``），实时切换靠它；
      * 拿不到（老 Qt、离屏平台插件返回 Unknown、没有 GUI app）时退回读
        ``AppsUseLightTheme`` 注册表值。

    两条路都失败就返回 "light"。**绝不抛异常** —— 这个函数在启动路径上，
    读不到系统主题不该让应用起不来。
    """
    try:
        from PySide6.QtCore import Qt
        from PySide6.QtGui import QGuiApplication
        app = QGuiApplication.instance()
        if app is not None:
            scheme = app.styleHints().colorScheme()
            if scheme == Qt.ColorScheme.Dark:
                return "dark"
            if scheme == Qt.ColorScheme.Light:
                return "light"
    except Exception:                                     # noqa: BLE001
        pass

    try:
        import winreg
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize",
        ) as key:
            value, _kind = winreg.QueryValueEx(key, "AppsUseLightTheme")
            return "light" if int(value) else "dark"
    except Exception:                                     # noqa: BLE001
        return "light"


def resolve_theme(theme: str) -> str:
    """把配置里的主题值解析成``"light"``/``"dark"``。

    **返回值一定不是 "system"** —— 这是本函数的契约。散在各处的消费点
    大多写成 ``x == "dark"`` 这种二元判断，一旦让它拿到 "system"，
    深色用户会静默掉回浅色（而且不报错，是最难查的那类 bug）。
    """
    name = str(theme or "").strip().lower()
    if name == SYSTEM_THEME:
        return system_theme()
    return "dark" if name == "dark" else "light"


def current_qss(theme: str) -> str:
    """按主题取样式表。入参允许直接是 ``"system"``。

    解析放在这里而不是让调用方各自处理：调用点有启动、设置页、跟随系统
    的定时回调三处，任何一处漏解析都会变成"深色用户看到浅色"。
    """
    return DARK_QSS if resolve_theme(theme) == "dark" else MAIN_QSS

