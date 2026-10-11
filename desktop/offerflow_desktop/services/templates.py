"""简历视觉模板：一套参数同时驱动 Word 与 PDF 导出。

成熟简历产品动辄几十上百套模板，本质差异其实就是这几组参数：
字号层级、页边距、行距、强调色、标题装饰方式、姓名对齐。
把它们抽成数据，导出层只负责解释，新增模板不需要改渲染代码。

**两条轴要分清**（2026-10-07 拆开）：

- **版式**（本文件的 ``ResumeTemplate``）：栏数、标题装饰、姓名对齐、
  各种段距比例 —— 决定「这份简历长什么样」。
- **外观**（``ResumeAppearance``）：强调色、字号整体缩放、中西文字体 ——
  决定「什么颜色、多大字」，与版式正交。

原来这两者被绑在同一个对象里，于是用户挑好配色之后换个版式就被覆盖掉。
现在换版式只改版式那半，外观由 ``apply_appearance()`` 单独叠加。

**ATS 标注不写死**：``ResumeTemplate.ats_notes`` 是从模板自身的数值**现算**的，
不是手填的布尔值。手填的字段改了字号/行距却忘记同步，就会变成
「模板自称安全、数字其实不安全」的假声明 —— 这正是本项目踩过的坑
（见错题本 E-09：说得出口 ≠ 真的成立）。阈值沿用开源实现
reactive-resume ``packages/resume/src/ats/rules.ts`` 里公开的常量口径。
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

# ATS 可读性阈值（口径来自参考实现 reactive-resume 的 ats/rules.ts：
# MIN_BODY_FONT_SIZE = 9、MIN_LINE_HEIGHT = 1.15）。
# 页边距那条（MIN_PAGE_MARGIN = 8pt）没有搬：本项目 6 套模板的 margin_cm
# 都在 1.2cm≈34pt 以上，写进来是一条永远不会触发的死规则。
ATS_MIN_BODY_PT = 9.0
ATS_MIN_LINE = 1.15


def _linear_luminance(rgb: Tuple[int, int, int]) -> float:
    """相对亮度（WCAG 口径），0=黑 1=白。

    必须先把 sRGB 分量反伽马成线性值再加权——直接拿 0..255 归一化后加权，
    会把中深色的亮度算高、对比度算低（例如 #808080 会被算成 1.9 而不是
    真实的 3.95），据此压深颜色会白白牺牲配色的鲜亮度。
    """
    def channel(c: int) -> float:
        s = c / 255.0
        return s / 12.92 if s <= 0.03928 else ((s + 0.055) / 1.055) ** 2.4

    r, g, b = (channel(c) for c in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _contrast(a: Tuple[int, int, int], b: Tuple[int, int, int]) -> float:
    la, lb = _linear_luminance(a), _linear_luminance(b)
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


def darken_for_text(hex_color: str, min_contrast: float = 4.5,
                    bg: Tuple[int, int, int] = (255, 255, 255)) -> str:
    """把强调色压深成「可直接当文字色」的版本。

    现有模板的主色（靛蓝 #4F46E5、青绿 #0F766E、蓝 #1D4ED8 等）在白底上
    分别有 6.29 / 5.47 / 6.70 的对比度，本来就满足 WCAG AA，所以这里对它们
    是恒等返回——配色按设计保持鲜亮。保留这道守卫是为了将来新增浅色强调色
    （如亮黄 #FBBF24，白底仅 1.5 左右）时，文字能自动压深到达标，
    而色块、横线、项目符号仍用原主色，视觉识别不受影响。
    """
    value = (hex_color or "").lstrip("#")
    if len(value) != 6:
        return hex_color
    try:
        rgb = tuple(int(value[i:i + 2], 16) for i in (0, 2, 4))
    except ValueError:
        return hex_color
    if _contrast(rgb, bg) >= min_contrast:
        return value.upper()
    # 在 sRGB 空间整体缩放到「刚好达标的最浅色」：k=1 是原色（不达标），
    # k=0 是纯黑（必然达标）。二分找最大的 k，避免一次性等比缩放压得过深、
    # 把色相也一起丢掉。
    lo, hi = 0.0, 1.0
    for _ in range(24):
        mid = (lo + hi) / 2.0
        trial = tuple(max(0, min(255, round(c * mid))) for c in rgb)
        if _contrast(trial, bg) >= min_contrast:
            lo = mid
        else:
            hi = mid
    scaled = tuple(max(0, min(255, round(c * lo))) for c in rgb)
    return "%02X%02X%02X" % scaled


class ResumeTemplate:
    """一套版式参数。"""

    def __init__(
        self,
        key: str,
        label: str,
        desc: str,
        *,
        accent_hex: str = "4F46E5",
        margin_cm: float = 1.4,
        name_pt: float = 20.0,
        position_pt: float = 10.5,
        contact_pt: float = 9.0,
        heading_pt: float = 11.5,
        body_pt: float = 10.5,
        line: float = 1.18,
        name_align: str = "center",
        heading_style: str = "underline",
        # 用 ●(U+25CF) 而非 •(U+2022)：实测黑体 simhei.ttf / 楷体 simkai.ttf
        # 都没有 U+2022 字形（U+25AA、U+25E6 同样缺失），一旦回退到这两个字体，
        # 项目符号就会渲染成空心方框。●/·/– 在全部候选中文字体里都齐备。
        bullet_char: str = "\u25cf",
        heading_before: float = 8.0,
        heading_after: float = 3.0,
        bullet_after: float = 1.5,
        block_after: float = 4.0,
        font_cn: str = "微软雅黑",
        font_en: str = "Arial",
        pdf_font_cn: Optional[str] = None,
        columns: int = 1,
        sidebar_sections: Tuple[str, ...] = (),
        sidebar_ratio: float = 0.34,
    ):
        self.key = key
        self.label = label
        self.desc = desc
        self.accent_hex = accent_hex
        # 文字专用色：原主色压深到达标对比度（姓名/求职意向/标题文字用）
        self.text_accent_hex = darken_for_text(accent_hex)
        # 栏数**由 sidebar_sections 推导**：声明了副栏分区 → 双栏；否则单栏。
        # 为什么不给一个「columns=2 开关」就算数：渲染器必须知道「哪些分区进副栏」，
        # 否则开了开关也只是把整段正文塞进左半页，看起来像排版事故。
        # columns 入参保留只为向后兼容（旧调用点显式传 1），实际以分区声明为准。
        self.sidebar_sections = tuple(sidebar_sections)
        self.sidebar_ratio = min(0.5, max(0.2, float(sidebar_ratio or 0.34)))
        self.columns = 2 if self.sidebar_sections else 1
        self.margin_cm = margin_cm
        self.name_pt = name_pt
        self.position_pt = position_pt
        self.contact_pt = contact_pt
        self.heading_pt = heading_pt
        self.body_pt = body_pt
        self.line = line
        self.name_align = name_align
        self.heading_style = heading_style      # underline / plain / block / center
        self.bullet_char = bullet_char
        self.heading_before = heading_before
        self.heading_after = heading_after
        self.bullet_after = bullet_after
        self.block_after = block_after
        self.font_cn = font_cn
        self.font_en = font_en
        self.pdf_font_cn = pdf_font_cn or font_cn

    @property
    def compact(self) -> bool:
        """是否为紧凑档（供旧代码判断）。"""
        return self.key == "compact"

    @property
    def layout_label(self) -> str:
        """版式的一句话描述，供 UI 显示。"""
        if self.columns == 1:
            return "单栏"
        return f"双栏（副栏 {len(self.sidebar_sections)} 个分区）"

    @property
    def ats_notes(self) -> List[str]:
        """这套版式对 ATS 可读性的影响 —— **从模板自身数值现算**。

        刻意不做成一个手填的字段：手填的 ``ats_safe`` 在有人把 body_pt 从
        10.5 改成 8.5 之后不会自动变，就成了「自称安全」的假声明。
        这里每次调用都按当前数值重算，所以 ``apply_appearance()`` 把字号
        缩到 0.85 之后它会自动多出一条提醒，不需要谁记得去改。

        返回值是给人看的中文短句列表，空列表 = 没有已知的 ATS 可读性风险。
        """
        notes: List[str] = []
        if self.columns != 1:
            notes.append(
                f"{self.columns} 栏排版，部分 ATS 会先读主栏、或与副栏交错读，"
                "关键词可能被拆散"
            )
        if self.body_pt < ATS_MIN_BODY_PT:
            notes.append(
                f"正文字号 {self.body_pt:.1f}pt 低于 {ATS_MIN_BODY_PT:.0f}pt，"
                "部分系统会判为过小"
            )
        if self.line < ATS_MIN_LINE:
            notes.append(
                f"行距 {self.line:.2f} 低于 {ATS_MIN_LINE:.2f}，正文容易粘连"
            )
        return notes

    @property
    def ats_safe(self) -> bool:
        """没有任何 ATS 可读性风险时为真。由 ats_notes 派生，二者永不矛盾。"""
        return not self.ats_notes

    def accent_rgb(self) -> tuple:
        value = self.accent_hex.lstrip("#")
        return tuple(int(value[i:i + 2], 16) for i in (0, 2, 4))

    def to_dict(self) -> Dict[str, object]:
        return {
            "key": self.key, "label": self.label, "desc": self.desc,
            "accent_hex": self.accent_hex,
            "text_accent_hex": self.text_accent_hex, "margin_cm": self.margin_cm,
            "name_pt": self.name_pt, "body_pt": self.body_pt,
            "line": self.line, "heading_style": self.heading_style,
            "name_align": self.name_align,
            "columns": self.columns,
            "sidebar_sections": list(self.sidebar_sections),
            "sidebar_ratio": self.sidebar_ratio,
            "ats_safe": self.ats_safe,
            "ats_notes": list(self.ats_notes),
        }


@dataclass(frozen=True)
class ResumeAppearance:
    """与版式解耦的外观：强调色、字号整体缩放、中西文字体。

    每一项都可以留空/取默认值，表示「沿用版式自带的那一项」——
    这样「只改颜色、版式的字体和疏密照旧」和「只改字号」都能表达。
    """

    accent_hex: Optional[str] = None
    font_scale: float = 1.0
    font_cn: Optional[str] = None
    font_en: Optional[str] = None

    # 字号缩放的安全区间。低于 0.85 会踩到 ATS 的 9pt 底线、高于 1.25
    # 正文就超过 13pt，看起来不像简历了。
    MIN_SCALE = 0.85
    MAX_SCALE = 1.25

    def normalized(self) -> "ResumeAppearance":
        """把外部输入（配置文件、UI 控件）收敛到合法区间。

        配置文件是可以被手工编辑的，历史版本也可能存过越界值，
        所以这里必须兜住：宁可静默取最近合法值，也不要把非法值带进排版。
        """
        accent = _normalize_hex(self.accent_hex)
        try:
            scale = float(self.font_scale)
        except (TypeError, ValueError):
            scale = 1.0
        if scale != scale or scale <= 0:          # NaN / 负数
            scale = 1.0
        scale = min(self.MAX_SCALE, max(self.MIN_SCALE, scale))
        return ResumeAppearance(
            accent_hex=accent,
            font_scale=round(scale, 3),
            font_cn=(self.font_cn or None),
            font_en=(self.font_en or None),
        )

    def is_default(self) -> bool:
        look = self.normalized()
        return (look.accent_hex is None and look.font_scale == 1.0
                and look.font_cn is None and look.font_en is None)

    def to_dict(self) -> Dict[str, Any]:
        look = self.normalized()
        return {
            "accent_hex": look.accent_hex or "",
            "font_scale": look.font_scale,
            "font_cn": look.font_cn or "",
            "font_en": look.font_en or "",
        }

    @classmethod
    def from_dict(cls, raw: Any) -> "ResumeAppearance":
        """从配置文件/接口数据还原。任何异常输入都退回默认外观。"""
        if not isinstance(raw, dict):
            return cls()
        return cls(
            accent_hex=raw.get("accent_hex") or None,
            font_scale=raw.get("font_scale", 1.0),
            font_cn=raw.get("font_cn") or None,
            font_en=raw.get("font_en") or None,
        ).normalized()


def _normalize_hex(value: Optional[str]) -> Optional[str]:
    """校验 6 位十六进制色值，非法一律当作「没设」。"""
    text = str(value or "").strip().lstrip("#")
    if len(text) != 6:
        return None
    try:
        int(text, 16)
    except ValueError:
        return None
    return text.upper()


# 外观只允许动这五个字号槽位。段距（heading_before/bullet_after/block_after）
# 按比例联动，否则「字号缩小、段距不变」会让版面显得稀稀拉拉。
_APPEARANCE_FONT_ATTRS = ("name_pt", "position_pt", "contact_pt", "heading_pt", "body_pt")
_APPEARANCE_GAP_ATTRS = ("heading_before", "heading_after", "bullet_after", "block_after")


def apply_appearance(tpl: ResumeTemplate,
                     appearance: Optional[ResumeAppearance]) -> ResumeTemplate:
    """把外观叠加到版式上，返回**新的**模板对象（绝不改原对象）。

    不改原对象是刻意的：``TEMPLATES`` 里那 6 个是模块级单例，
    就地改会把「这一次导出用的颜色」永久渗进所有后续导出。

    只碰三类东西 —— 强调色、字号（连带段距按比例）、字体。
    栏数、标题装饰、姓名对齐、边距这些属于版式，一概不动，
    所以用户换版式时调好的外观能原样跟过去。
    """
    if appearance is None:
        return tpl
    look = appearance.normalized()
    if look.is_default():
        return tpl

    out = copy.copy(tpl)
    if look.accent_hex:
        out.accent_hex = look.accent_hex
        # 文字用色跟着重算，才能保住 WCAG 对比度守卫
        out.text_accent_hex = darken_for_text(look.accent_hex)
    if look.font_scale != 1.0:
        for attr in _APPEARANCE_FONT_ATTRS:
            setattr(out, attr, round(getattr(tpl, attr) * look.font_scale, 2))
        # 段距比字号更敏感：乘 1.4 次幂，让缩小字号时段距收得比字号少，
        # 避免出现「字小了、空行还很宽」的松散感。
        factor = round(look.font_scale ** 1.4, 4)
        for attr in _APPEARANCE_GAP_ATTRS:
            setattr(out, attr, round(getattr(tpl, attr) * factor, 2))
    if look.font_cn:
        out.font_cn = look.font_cn
        out.pdf_font_cn = look.font_cn
    if look.font_en:
        out.font_en = look.font_en
    return out


def resolve_template(template_key: Optional[str],
                     appearance: Optional[ResumeAppearance] = None,
                     order: Optional[Any] = None) -> ResumeTemplate:
    """导出与预览共用的唯一入口：取版式 → 叠外观。

    ``order`` 参数这里不参与模板解析，只为了让「取版式」这件事有一个
    统一的入口签名——调用方拿到模板和顺序之后一起往下传，
    免得散落的 ``get_template()`` 调用各自忘记叠外观。
    """
    return apply_appearance(get_template(template_key), appearance)


TEMPLATES: List[ResumeTemplate] = [
    ResumeTemplate(
        "classic", "经典单栏", "靛蓝强调 + 标题下划线，投递通用",
        accent_hex="4F46E5", margin_cm=1.4,
        name_pt=20, position_pt=10.5, contact_pt=9.0, heading_pt=11.5, body_pt=10.5,
        line=1.18, name_align="center", heading_style="underline",
    ),
    ResumeTemplate(
        "compact", "紧凑一页", "字号边距收紧，内容多时守住一页",
        accent_hex="4F46E5", margin_cm=1.2,
        name_pt=18, position_pt=10, contact_pt=8.5, heading_pt=11, body_pt=10,
        line=1.08, name_align="center", heading_style="underline",
        heading_before=5, heading_after=2, bullet_after=0.5, block_after=2,
    ),
    ResumeTemplate(
        "elegant", "雅致留白", "姓名左对齐 + 无边框标题，行距宽松",
        accent_hex="334155", margin_cm=1.8,
        name_pt=22, position_pt=10.5, contact_pt=9, heading_pt=11, body_pt=10.5,
        line=1.5, name_align="left", heading_style="plain",
        heading_before=13, heading_after=4, bullet_after=2.5, block_after=6,
    ),
    ResumeTemplate(
        "tech", "技术岗", "青绿强调 + 标题色块，技能分组醒目",
        accent_hex="0F766E", margin_cm=1.3,
        name_pt=19, position_pt=10, contact_pt=8.5, heading_pt=11, body_pt=10,
        line=1.25, name_align="center", heading_style="block",
        font_en="Consolas", heading_before=11, heading_after=5, block_after=6,
    ),
    ResumeTemplate(
        "academic", "学术简历", "宋体衬线 + 居中标题，适合科研与升学",
        accent_hex="1F2937", margin_cm=1.6,
        name_pt=18, position_pt=10.5, contact_pt=9, heading_pt=11, body_pt=10.5,
        line=1.35, name_align="center", heading_style="center",
        font_cn="宋体", font_en="Times New Roman", pdf_font_cn="宋体",
        heading_before=10, block_after=5,
    ),
    ResumeTemplate(
        "bold", "商务气场", "大号姓名 + 深蓝色块标题，气场更强",
        accent_hex="1D4ED8", margin_cm=1.5,
        name_pt=24, position_pt=11, contact_pt=9, heading_pt=12, body_pt=10.5,
        line=1.2, name_align="center", heading_style="block",
        heading_before=12, heading_after=5, block_after=6,
    ),
    # 双栏：主栏放经历，副栏放技能/证书/荣誉这类短条目。
    # ATS 取舍见 ats_notes —— 副栏排版会被部分系统交错读，所以默认把
    # 「经历」留在主栏，只把可枚举的短分区挪到副栏，降低关键词被拆散的风险。
    ResumeTemplate(
        "split", "双栏·副栏技能", "主栏经历 + 副栏技能证书，信息密度高",
        accent_hex="0F766E", margin_cm=1.2,
        name_pt=18, position_pt=10, contact_pt=8.5, heading_pt=11, body_pt=10,
        line=1.2, name_align="left", heading_style="block",
        heading_before=9, heading_after=4, block_after=5,
        sidebar_sections=("skills", "certifications", "honors"),
        sidebar_ratio=0.34,
    ),
]

TEMPLATE_MAP: Dict[str, ResumeTemplate] = {t.key: t for t in TEMPLATES}
DEFAULT_TEMPLATE_KEY = "classic"


def get_template(key: Optional[str]) -> ResumeTemplate:
    """按 key 取模板，未知 key 回退到默认（永远不抛异常，导出流程要稳）。"""
    if key and key in TEMPLATE_MAP:
        return TEMPLATE_MAP[key]
    return TEMPLATE_MAP[DEFAULT_TEMPLATE_KEY]


def template_choices(appearance: Optional[ResumeAppearance] = None
                     ) -> List[Dict[str, Any]]:
    """供 UI 展示的模板清单（含能力声明）。

    接受 ``appearance`` 是因为 ATS 标注是从**有效模板**现算的：
    同一套紧凑版式，字号缩到 0.85 之后就会多出一条「低于 9pt」的风险，
    清单里应该如实反映，而不是永远显示版式出厂时的那一套结论。
    """
    out: List[Dict[str, Any]] = []
    for tpl in TEMPLATES:
        effective = apply_appearance(tpl, appearance)
        out.append({
            "key": effective.key,
            "label": effective.label,
            "desc": effective.desc,
            "columns": effective.columns,
            "layout_label": effective.layout_label,
            "ats_safe": effective.ats_safe,
            "ats_notes": list(effective.ats_notes),
        })
    return out
