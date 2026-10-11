# -*- coding: utf-8 -*-
"""设计令牌（Design Tokens）—— 桌面端视觉规范的唯一事实来源。

背景：改造前 ui/ 下有 **115 种颜色、39 种卡片内边距、11 种间距、53 处内联字号**，
都是各页面随手写死的魔数。本文件把这些收敛成有限档位，页面代码一律引用令牌，
不再出现裸数字。

档位设计原则：
  * 间距/圆角一律 **4 的倍数**，形成可感知的节奏（4 / 8 / 12 / 16 / 20 / 24）
  * 语义命名（PAGE_ / CARD_ / SP_），不按像素值命名，便于整体调档
  * 颜色按**语义**分类（text/bg/border/brand/semantic），数据可视化配色不进来
"""

from __future__ import annotations

# ============================================================
# 间距（4 的倍数）
# ============================================================
SP_XS = 4     # 同一行内紧密元素（图标与文字、标签与其控件）
SP_SM = 8     # 卡片内部相邻元素
SP_MD = 12    # 卡片内部的分组之间
SP_LG = 16    # 页面内卡片之间
SP_XL = 20    # 大区块之间

# ============================================================
# 内边距
# ============================================================
# 页面根容器（内容区四周）
PAGE_MARGINS = (24, 20, 24, 20)
# 标准卡片
CARD_MARGINS = (16, 14, 16, 14)
# 嵌套小卡 / 紧凑卡
CARD_MARGINS_SM = (12, 10, 12, 10)
# 无内边距
ZERO_MARGINS = (0, 0, 0, 0)

# ============================================================
# 侧栏导航
# ============================================================
# 宽度：从 214 提到 252 —— 条目要放下「主标题 + 副标题」两行，
# 214px 下副标题会被截成「没有简历，从零做…」这种半句话，反而不如没有。
NAV_W = 252
NAV_ROW_H = 44            # 导航条目（两行：主标题 + 副标题）
NAV_ICON = 20             # 导航图标
NAV_INDICATOR_W = 3       # 选中态左侧竖条
NAV_SIDE_PAD = 10         # 侧栏内容左右内边距
NAV_ROW_GAP = 2           # 条目间距
NAV_AVATAR = 32           # 用户卡头像直径

# ============================================================
# 圆角
# ============================================================
R_SM = 4      # 小元素（checkbox、tag）
R_MD = 6      # 按钮、输入框
R_LG = 8      # 卡片、列表、标签页面板
R_PILL = 15   # 胶囊（chip）

# ============================================================
# 字号（对应 QSS 里的字号档位，页面不再写内联 font-size）
# ============================================================
FS_PAGE_TITLE = 20   # 页面主标题
FS_SECTION = 14      # 区块标题（section-title）
FS_CARD_TITLE = 15   # 卡片内强调标题 / 数值
FS_BODY = 13         # 正文（默认，无需显式设置）
FS_HINT = 12         # 辅助说明 / 次要信息（hint / muted）
FS_TINY = 11         # 极次要（版本号、角标）
FS_SCORE = 26        # 大号评分数字

# ============================================================
# 语义颜色（浅色主题）
# ============================================================
class C:
    """浅色主题语义色板。

    注意：**不含**图表/数据可视化配色 —— 那类颜色随数据语义走，
    不参与视觉规范收敛（例如 cost.py 的 OPERATION_COLORS）。
    """

    # 品牌
    BRAND = "#4f46e5"
    BRAND_HOVER = "#4338ca"
    BRAND_SOFT = "#eef0fe"        # 选中态浅底
    BRAND_TEXT = "#4f46e5"

    # 文字
    TEXT = "#1a1a2e"
    TEXT_SECONDARY = "#5f6470"
    TEXT_HINT = "#6b7280"
    TEXT_MUTED = "#9ca3af"
    TEXT_DISABLED = "#c3c7d0"

    # 背景
    BG_APP = "#f7f8fa"
    BG_CARD = "#ffffff"
    BG_HOVER = "#f5f6fa"
    BG_SUBTLE = "#f1f2f6"         # 次级底（chip 未选、表头）
    BG_HEADER = "#fafafa"

    # 边框
    BORDER = "#e8eaf0"
    BORDER_STRONG = "#d6d9e0"     # 输入框、按钮描边
    BORDER_SOFT = "#ececf1"

    # 语义
    SUCCESS = "#16a34a"
    SUCCESS_HOVER = "#15803d"
    SUCCESS_BORDER = "#dcefe6"
    WARN = "#f59e0b"
    WARN_BORDER = "#f0e4c8"
    DANGER = "#dc2626"
    DANGER_HOVER = "#ef4444"
    DANGER_BORDER = "#f3d7d7"
    DANGER_TEXT = "#dc2626"
    INFO = "#06b6d4"


# ============================================================
# 旧值 → 令牌值 的收敛映射（供批量规范化脚本使用）
# ============================================================

# setContentsMargins 收敛：39 种 → 4 种
#   判据：页面/卡片内边距本应只有「页面级 / 标准卡 / 小卡 / 零」四档
MARGINS_MAP = {
    (14, 10, 14, 10): CARD_MARGINS,
    (18, 14, 18, 14): CARD_MARGINS,
    (14, 12, 14, 12): CARD_MARGINS,
    (18, 16, 18, 16): CARD_MARGINS,
    (16, 12, 16, 12): CARD_MARGINS,
    (18, 12, 18, 12): CARD_MARGINS,
    (20, 16, 20, 16): CARD_MARGINS,
    (16, 16, 16, 16): CARD_MARGINS,
    (16, 14, 16, 14): CARD_MARGINS,

    (12, 8, 12, 8): CARD_MARGINS_SM,
    (12, 12, 12, 12): CARD_MARGINS_SM,
    (10, 8, 10, 8): CARD_MARGINS_SM,
    (8, 8, 8, 8): CARD_MARGINS_SM,
    (10, 10, 10, 10): CARD_MARGINS_SM,
    (12, 10, 12, 10): CARD_MARGINS_SM,

    (24, 20, 24, 20): PAGE_MARGINS,
    (24, 20, 24, 16): PAGE_MARGINS,
    (24, 20, 24, 8): PAGE_MARGINS,
    (24, 8, 24, 20): PAGE_MARGINS,

    (0, 0, 0, 0): ZERO_MARGINS,
}

# setSpacing 收敛：11 种 → 5 种（一律 4 的倍数）
SPACING_MAP = {
    2: SP_XS,
    3: SP_XS,
    4: SP_XS,
    6: SP_SM,
    7: SP_SM,
    8: SP_SM,
    10: SP_MD,
    12: SP_MD,
    14: SP_LG,
    18: SP_LG,
    16: SP_LG,
    0: 0,
}

# 内联 font-size 收敛：6 种 → 4 种
FONTSIZE_MAP = {
    "11px": FS_TINY,
    "12px": FS_HINT,
    "13px": FS_BODY,
    "14px": FS_SECTION,
    "15px": FS_CARD_TITLE,
    "16px": FS_CARD_TITLE,
    "20px": FS_PAGE_TITLE,
    "26px": FS_SCORE,
}


def margins_str(m) -> str:
    return ", ".join(str(v) for v in m)


# ============================================================
# 当前主题状态
# ============================================================
# 用途：QPainter 绘制（icons.py）与无法用 QSS class 表达的动态色（状态色板）
# 需要知道当前主题。QSS 能自动跟随主题重刷，但**内联样式与手绘颜色不会**，
# 因此这里维护一份进程内的主题状态，由切换主题处调用 set_theme() 同步。

_theme_override: str | None = None


def set_theme(name: str | None) -> None:
    """切换主题时调用（system_settings / main_window 启动时）。"""
    global _theme_override
    _theme_override = name


def is_dark() -> bool:
    """当前是否为深色主题。优先用显式设置，否则读系统配置。

    两条路都必须过 ``theme.resolve_theme``：配置里可能存的是 ``"system"``，
    直接拿去和 ``"dark"`` 比会得到 False —— 系统是深色时，QSS 变深了、
    手绘图标与状态色板却还按浅色画，界面会花。
    """
    from . import theme as _theme
    if _theme_override:
        return _theme.resolve_theme(_theme_override) == "dark"
    try:
        from .. import database as db
        return _theme.resolve_theme(db.get_system_config().get("theme")) == "dark"
    except Exception:  # noqa: BLE001
        return False


def spacing_token(v: int) -> str:
    """把间距值转成令牌常量名，便于脚本生成 `setSpacing(tokens.SP_SM)`。"""
    return {
        0: "0",
        SP_XS: "SP_XS",
        SP_SM: "SP_SM",
        SP_MD: "SP_MD",
        SP_LG: "SP_LG",
        SP_XL: "SP_XL",
    }.get(v, str(v))
