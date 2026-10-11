"""简历导出服务：PDF / Word（中文排版 + 一页硬约束）。

产品硬性要求与实现要点：

1. **一页硬约束**——导出时用实际渲染测页数，超一页就逐级缩小字号/行距/边距，
   直到塞进一页；内容完整不删减。内容偏少时反向补齐：自动放大模块间距，
   让版面铺得饱满而不是全部堆在页顶。
2. **跨环境字体一致**——按「Linux 容器 / Windows 开发机」候选链自动探测中文字体，
   可用 RESUME_FONT_PATH 覆盖（便于本地预览直接用线上同一字体文件）。
3. **无粗体字重时合成加粗**——容器里的文泉驿只有 Regular，没有 Bold 文件；
   命中真实粗体才用真字形，否则用「填充+描边」（Tr=2）合成视觉重量。
4. **只使用跨字体安全的符号**：容器文泉驿、Windows 黑体/雅黑**都缺 U+2022（•）**，
   一旦使用就会渲染成空心方框。因此项目符号统一用 ●(U+25CF)、
   行内分隔用 ·(U+00B7)、日期区间用 –(U+2013)——这三个字形全环境齐备。
5. **默认单栏 ATS 友好排版**，可选双栏（``layout="split"``）：主栏放经历，
   副栏放技能/证书这类可枚举的短分区。双栏不是「打开一个开关」——必须
   同时声明**哪些分区进副栏**，否则只是把整段正文挤进左半页，看着像排版事故。
   双栏的 ATS 取舍在 ``ats_notes()`` 里如实说明，不做「自称安全」的假声明。
"""

import base64
import io
import logging
import os
import re
from typing import List, Optional, Tuple

from reportlab.lib import colors
from reportlab.lib.enums import TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.lib.utils import ImageReader
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas as rl_canvas
from reportlab.platypus import (
    HRFlowable,
    Image as RLImage,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)
from reportlab.platypus.doctemplate import LayoutError
from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_TAB_ALIGNMENT
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Cm, Pt, RGBColor

from app.models.resume import Resume
from app.modules.resume.structure import StructuredResume, parse_resume_structure

logger = logging.getLogger(__name__)


# ============ 跨字体安全符号 ============
# 实测结论（simhei/simsun/Deng/msyh/文泉驿全部缺 U+2022）：
#   项目符号 •(U+2022) -> 空心方框，禁止使用
#   项目符号 ●(U+25CF) -> 全环境齐备
#   行内分隔 ·(U+00B7) -> 全环境齐备
#   日期区间 –(U+2013) -> 全环境齐备
BULLET = "\u25CF"
MIDDOT = " \u00B7 "
EN_DASH = "\u2013"


# ============ 配色（克制：主色仅用于标题/强调，正文保持灰阶） ============

_INK = colors.HexColor("#0F172A")        # 最深：姓名、条目标题
_PRIMARY = colors.HexColor("#1E3A8A")    # 主色：模块标题、圆点、量化数字
_TEXT = colors.HexColor("#334155")       # 正文
_MUTED = colors.HexColor("#64748B")      # 次要：日期、联系方式、子行
_RULE = colors.HexColor("#E2E8F0")       # 浅分隔线

_PRIMARY_HEX = "#1E3A8A"
_ACCENT_HEX = "#1E3A8A"                  # 量化数字强调色

# 合成加粗的描边宽度 = 字号 × 该系数
_EMBOSS_WIDTH_RATIO = 0.035


# ============ 中文字体注册 ============

# (键, 常规字体路径, 粗体字体路径, TTC 子字体索引)
# 顺序即优先级：容器（Linux）优先，其次是本地开发机（Windows）。
_FONT_CANDIDATES: List[Tuple[str, str, Optional[str], int]] = [
    ("wqy-microhei", "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc", None, 0),
    ("msyh", "C:/Windows/Fonts/msyh.ttc", "C:/Windows/Fonts/msyhbd.ttc", 0),
    ("wqy-zenhei", "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc", None, 0),
    ("simhei", "C:/Windows/Fonts/simhei.ttf", None, 0),
    ("dejavu", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", None, 0),
]

_FONT_NAME = "CN"
_FONT_BOLD = "CN-Bd"

_FONT_STATE = {"base": "Helvetica", "bold": "Helvetica", "real_bold": False, "ready": False}


def _font_candidates() -> List[Tuple[str, str, Optional[str], int]]:
    """环境变量 RESUME_FONT_PATH 可覆盖首选字体，便于本地预览对齐线上。"""
    override = os.getenv("RESUME_FONT_PATH", "").strip()
    if not override:
        return _FONT_CANDIDATES
    bold = os.getenv("RESUME_FONT_BOLD_PATH", "").strip() or None
    try:
        index = int(os.getenv("RESUME_FONT_INDEX", "0") or 0)
    except ValueError:
        index = 0
    return [("override", override, bold, index)] + _FONT_CANDIDATES


def _register_font() -> str:
    """探测并注册中文字体，返回正文字体名。

    同时注册 `<字体>-Bd` 作为粗体名，并通过 registerFontFamily 让
    Paragraph 里的 <b> 标签自动切到它。找不到任何中文字体时回退 Helvetica。
    """
    if _FONT_STATE["ready"]:
        return _FONT_STATE["base"]

    for _key, regular, bold, index in _font_candidates():
        if not regular or not os.path.exists(regular):
            continue
        try:
            pdfmetrics.registerFont(TTFont(_FONT_NAME, regular, subfontIndex=index))
        except Exception:
            continue

        real_bold = False
        if bold and os.path.exists(bold):
            try:
                pdfmetrics.registerFont(TTFont(_FONT_BOLD, bold, subfontIndex=index))
                real_bold = True
            except Exception:
                real_bold = False
        if not real_bold:
            # 没有粗体文件：同一字形再注册一份，交由 _EmbossCanvas 用描边加粗
            try:
                pdfmetrics.registerFont(TTFont(_FONT_BOLD, regular, subfontIndex=index))
            except Exception:
                pass

        try:
            pdfmetrics.registerFontFamily(
                _FONT_NAME,
                normal=_FONT_NAME,
                bold=_FONT_BOLD,
                italic=_FONT_NAME,
                boldItalic=_FONT_BOLD,
            )
        except Exception:
            pass

        _FONT_STATE.update({"base": _FONT_NAME, "bold": _FONT_BOLD,
                            "real_bold": real_bold, "ready": True})
        return _FONT_NAME

    _FONT_STATE.update({"base": "Helvetica", "bold": "Helvetica",
                        "real_bold": False, "ready": True})
    return "Helvetica"


class _EmbossCanvas(rl_canvas.Canvas):
    """合成加粗：遇到 `-Bd` 字体时切到「填充+描边」，把笔画视觉加粗。

    只在中文字体没有真实 Bold 字形时生效；命中真实粗体（如 msyhbd）时不再叠加，
    否则会出现糊边的「假肥」。
    """

    def setFont(self, psfontname, size, leading=None):
        result = rl_canvas.Canvas.setFont(self, psfontname, size, leading)
        if _FONT_STATE["real_bold"]:
            return result
        try:
            if str(psfontname).endswith("-Bd"):
                self.setLineWidth(max(float(size) * _EMBOSS_WIDTH_RATIO, 0.2))
                self.setStrokeColor(self._fillColorObj)
                self.setTextRenderMode(2)
            else:
                self.setTextRenderMode(0)
        except Exception:
            pass
        return result


# ============ 文本处理 ============

def _bullet_line(line: str) -> str:
    """去掉行首的项目符号，返回纯文本。"""
    return re.sub(r"^\s*(?:[-•·▪◦●*▸▶►]\s*)+", "", line).strip()


def _split_bullets(lines: List[str]) -> List[str]:
    """把可能含多个 `-` 分项的单行拆成多条。"""
    result: List[str] = []
    for line in lines:
        parts = re.split(r"\s{2,}(?=[-•·▪◦●*▸▶►]\s)", line)
        result.extend(p for p in parts if p.strip())
    return result


def _escape(text: str) -> str:
    """转义 reportlab Paragraph 的特殊字符。

    只做转义，不加任何标签；调用方若要加 `<b>` 需在转义之后再拼。
    """
    return (
        (text or "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def _bold(text: str) -> str:
    """安全加粗：先转义再包 <b>，避免标签被转义掉。"""
    return f"<b>{_escape(text)}</b>" if text else ""


# 量化表达：百分比、带单位的数字（简历的「成果重点」），用于加色突出
_QUANT_PATTERN = re.compile(
    r"\d+(?:\.\d+)?\s*(?:%|万|千|亿|元|人|个|倍|条|项|次|ms|s|qps|k|w|月|日|年)",
    re.IGNORECASE,
)


def _highlight_quantities(text: str) -> str:
    """给量化数字/百分比上色，突出简历的成果重点。

    需在 _escape 之后调用，这样插入的 <font> 标签不会被转义。
    """
    return _QUANT_PATTERN.sub(
        lambda m: f'<font color="{_ACCENT_HEX}">{m.group(0)}</font>',
        text,
    )


# ============ 一页自适应 ============

# 字号梯度：(正文, 行距, 模块标题, 辅助字号, 姓名, 左右边距mm)
# 从第一档（最舒展、字号最大）开始试，超一页就降一档。
# 层级遵循成熟简历规范：姓名 ≥20pt > 模块标题 12pt > 正文 10pt，左对齐不居中。
_SIZE_TIERS: List[Tuple[float, float, float, float, float, float]] = [
    (12.0, 19.0, 14.5, 11.0, 24.0, 22),
    (11.5, 18.0, 14.0, 10.5, 22.5, 21),
    (11.0, 17.0, 13.0, 10.0, 21.0, 20),
    (10.5, 16.5, 12.5, 9.5, 20.0, 19),
    (10.0, 15.5, 12.0, 9.5, 18.5, 18),
    (9.5, 14.5, 11.5, 9.0, 17.5, 16),
    (9.0, 13.5, 11.0, 9.0, 16.5, 15),
    (8.5, 13.0, 10.5, 8.5, 15.5, 14),
    (8.0, 12.0, 10.0, 8.5, 14.5, 12),
]

_WORD_SIZE_TIERS: List[float] = [10.5, 10.0, 9.5, 9.0, 8.5]

_TOP_MARGIN_MM = 14.0
_BOTTOM_MARGIN_MM = 14.0

# 内容偏少时的「舒展」上限：先把模块间距补满，再把行距适度放开。
# 上限刻意收得比较紧——间距拉过头会变成「松散」而不是「饱满」。
_MAX_MODULE_GAP = 16.0    # 每个模块额外间距的基准上限（pt）
_MAX_MODULE_GAP_EXTRA = 16.0  # 内容越少，允许的间距越疏朗，最多再加这么多
_GAP_RELAX_AT = 0.85      # 内容自然高度达到可用高度的这个比例时，不再额外放宽间距
_MAX_LEAD_SCALE = 1.12    # 行距最多放大到 1.12 倍
_TARGET_FILL = 0.95       # 内容铺到可用高度的百分之多少
_FIRST_GAP_CAP = 12.0     # 页头到第一个模块的间距上限，避免页头下方出现大空洞
# 简历按惯例顶对齐，所以只在内容「极空」时才补一点顶部呼吸空间，
# 避免版面变成上下两张空白的「名片」。余量低于阈值就完全不补。
_TOP_PAD_RATIO = 0.50     # 超出的余量里补多少到页头之上
_TOP_PAD_MIN_SLACK = 260.0  # 余量低于此值不补顶部（正常内容量的简历不受影响）
_TOP_PAD_CAP = 70.0       # 顶部补白上限


def _styles(
    font: str,
    body: float,
    leading: float,
    heading: float,
    contact_size: float,
    title_size: float,
    extra_gap: float = 0.0,
    lead_scale: float = 1.0,
) -> dict:
    """构建整套段落样式。两条渲染路径（结构化 / 纯文本兜底）共用，保证观感一致。

    extra_gap  —— 内容偏少时补在模块之间的间距
    lead_scale —— 内容偏少时整体放开行距的系数（1.0 为原始行距）
    """
    ls = lead_scale
    primary_gap = contact_size + 2.0
    return {
        # 模块间距不能挂在 heading 的 spaceBefore 上：模块标题为了画左侧竖条
        # 被包进了 Table，而 Table 单元格会忽略 Paragraph 的 spaceBefore/After。
        # 因此间距改由 story 里显式插入的 Spacer 承担（见 _heading_flowable）。
        "module_gap": 8 + extra_gap,
        # 页头到第一个模块留窄一点，否则页头下方会出现一块突兀的空白
        "module_gap_first": min(8 + extra_gap, _FIRST_GAP_CAP),
        "name": ParagraphStyle(
            "CName", fontName=font, fontSize=title_size, leading=title_size * 1.22 * ls,
            spaceAfter=2, textColor=_INK,
        ),
        "position": ParagraphStyle(
            "CPosition", fontName=font, fontSize=primary_gap,
            leading=primary_gap * 1.45 * ls, spaceAfter=1, textColor=_PRIMARY,
        ),
        "contact": ParagraphStyle(
            "CContact", fontName=font, fontSize=contact_size, leading=contact_size * 1.5 * ls,
            spaceAfter=2, textColor=_MUTED,
        ),
        "heading": ParagraphStyle(
            "CHeading", fontName=font, fontSize=heading, leading=heading * 1.3 * ls,
            spaceBefore=0, spaceAfter=0, textColor=_PRIMARY,
        ),
        "summary": ParagraphStyle(
            "CSummary", fontName=font, fontSize=body, leading=leading * ls,
            spaceAfter=1.5, textColor=_TEXT,
        ),
        "item_title": ParagraphStyle(
            "CItemTitle", fontName=font, fontSize=body + 0.5, leading=(body + 0.5) * 1.35 * ls,
            spaceBefore=3, spaceAfter=1, textColor=_INK,
        ),
        "time": ParagraphStyle(
            "CTime", fontName=font, fontSize=body - 0.5, leading=(body - 0.5) * 1.4 * ls,
            alignment=TA_RIGHT, textColor=_MUTED,
        ),
        "item_sub": ParagraphStyle(
            "CItemSub", fontName=font, fontSize=body - 0.5, leading=(body - 0.5) * 1.45 * ls,
            leftIndent=body * 0.8, spaceAfter=1, textColor=_MUTED,
        ),
        # 圆点用 bulletText 绘制，reportlab 会自动做悬挂缩进：
        # 续行对齐 leftIndent，圆点落在 bulletIndent。
        "bullet": ParagraphStyle(
            "CBullet", fontName=font, fontSize=body, leading=leading * ls,
            leftIndent=body * 1.3, bulletIndent=body * 0.15,
            bulletFontName=font, bulletFontSize=body * 0.62, bulletColor=_PRIMARY,
            spaceAfter=1.5, textColor=_TEXT,
        ),
        "plain": ParagraphStyle(
            "CPlain", fontName=font, fontSize=body, leading=leading * ls,
            spaceAfter=1.5, textColor=_TEXT,
        ),
        "detail": ParagraphStyle(
            "CDetail", fontName=font, fontSize=body - 0.5, leading=(body - 0.5) * 1.45 * ls,
            leftIndent=body * 0.8, spaceAfter=1, textColor=_MUTED,
        ),
    }


def _heading_flowable(title: str, st: dict, width_pt: float, heading_size: float,
                      first: bool = False):
    """模块标题：间距 + 左侧主色竖条 + 加粗标题。

    返回可直接 extend 进 story 的 flowable 列表。间距用 Spacer 显式表达，
    而不是 heading 的 spaceBefore——因为标题被包在 Table 里，单元格会忽略它。
    """
    bar = Table(
        [[Paragraph(_bold(title), st["heading"])]],
        colWidths=[width_pt],
    )
    bar.setStyle(TableStyle([
        ("LINEBEFORE", (0, 0), (0, -1), max(heading_size * 0.2, 2.0), _PRIMARY),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
        ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ("TOPPADDING", (0, 0), (-1, -1), 0),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
    ]))
    gap = st["module_gap_first"] if first else st["module_gap"]
    return [Spacer(1, gap), bar]


def _exp_header_row(left_html: str, right_html: str, left_style, right_style, width_mm: float):
    """经历标题行：项目名+角色在左，时间右对齐。"""
    tbl = Table(
        [[Paragraph(left_html, left_style), Paragraph(right_html, right_style)]],
        colWidths=[width_mm * 0.72 * mm, width_mm * 0.28 * mm],
    )
    tbl.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ("TOPPADDING", (0, 0), (-1, -1), 2),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
    ]))
    return tbl


# ============ 版式：单栏 / 双栏 ============

# 版式注册表：key -> 需要放进**副栏**的分区名。
# 值里的名字既当「结构化渲染的分区标签」，也当「纯文本兜底路径的标题关键词」
# 用（见 _goes_sidebar）——这样两条渲染路径共用一份版式定义，不会各说各话。
# 空元组 = 单栏，与改造前的行为逐字节一致。
LAYOUTS: dict = {
    "single": (),
    # 副栏只放可枚举的短分区：经历类内容留在主栏，降低关键词被 ATS 交错读散的风险。
    "split": ("技能", "证书"),
}
DEFAULT_LAYOUT = "single"

# 版式的 UI 文案。单独放一张表，再由它和 LAYOUTS 派生出对外的 LAYOUT_CHOICES：
# 加了版式却忘了写文案的情况会在**导入期**直接炸掉，而不是让前端拿到空标签。
_LAYOUT_META = {
    "single": ("单栏", "ATS 最友好，投递通用（PDF / Word 均支持）"),
    "split": ("双栏 · 副栏技能", "主栏经历 + 副栏技能证书，信息密度更高（仅 PDF）"),
}

if set(LAYOUTS) != set(_LAYOUT_META):
    raise RuntimeError("LAYOUTS 与 _LAYOUT_META 的 key 不一致，请同步补齐版式文案")

LAYOUT_CHOICES: List[dict] = [
    {
        "key": key,
        "label": _LAYOUT_META[key][0],
        "desc": _LAYOUT_META[key][1],
        "columns": 2 if LAYOUTS[key] else 1,
        "sidebar_sections": list(LAYOUTS[key]),
    }
    for key in LAYOUTS
]

_DEFAULT_SIDEBAR_RATIO = 0.34   # 副栏占版心宽度的比例
_MIN_SIDEBAR_RATIO = 0.2
_MAX_SIDEBAR_RATIO = 0.5
_COLUMN_GUTTER_PT = 12.0        # 两栏之间的栏间距（pt）


def resolve_layout(layout: Optional[str]) -> Tuple[str, Tuple[str, ...]]:
    """把外部传入的 layout 收敛到合法值，返回 (key, 副栏分区)。

    未知 key 一律回退单栏而不是抛异常：导出流程要稳，版式参数写错
    不该让用户连一份 PDF 都拿不到。
    """
    key = str(layout or DEFAULT_LAYOUT).strip().lower()
    if key not in LAYOUTS:
        key = DEFAULT_LAYOUT
    return key, tuple(LAYOUTS[key])


def _column_widths(content_w: float, sidebar_sections: Tuple[str, ...],
                   sidebar_ratio: float) -> Tuple[float, float]:
    """按比例切分版心宽度；单栏时两栏都返回整幅版心。"""
    if not sidebar_sections:
        return content_w, content_w
    try:
        ratio = float(sidebar_ratio)
    except (TypeError, ValueError):
        ratio = _DEFAULT_SIDEBAR_RATIO
    if ratio != ratio:                                 # NaN
        ratio = _DEFAULT_SIDEBAR_RATIO
    ratio = min(_MAX_SIDEBAR_RATIO, max(_MIN_SIDEBAR_RATIO, ratio))
    side_w = content_w * ratio - _COLUMN_GUTTER_PT / 2.0
    main_w = content_w * (1.0 - ratio) - _COLUMN_GUTTER_PT / 2.0
    return main_w, side_w


def _goes_sidebar(label: str, sidebar_sections: Tuple[str, ...]) -> bool:
    """该分区是否进副栏。

    用「包含」而不是「相等」：结构化路径的标签是固定的（技能/证书），
    但纯文本兜底路径的小节标题是从用户文档里原样抠出来的，
    可能是「技能特长」「专业技能」这类变体。
    """
    if not sidebar_sections:
        return False
    return any(token in (label or "") for token in sidebar_sections)


def _effective_sidebar(sidebar_sections: Tuple[str, ...],
                       labels: List[str]) -> Tuple[str, ...]:
    """把「声明要进副栏的分区」收敛成「这份简历里**真的存在**且要进副栏的分区」。

    必须在这里过滤，而不是等排版时发现副栏是空的再补救：栏宽是按
    ``sidebar_sections`` 提前算好的，若声明了一个本文档里根本没有的分区
    （比如让一份没写语言的简历把「语言」放副栏），主栏会被硬生生压窄到
    66%，渲染出来是一列窄条——比单栏难看得多。先证明有内容再分栏。
    """
    if not sidebar_sections:
        return ()
    return tuple(token for token in sidebar_sections
                 if any(token in (label or "") for label in labels))


def _structured_section_labels(s: dict) -> List[str]:
    """结构化简历里实际会出现的分区标签（与 ``_pdf_story_structured`` 同源）。

    判据必须和渲染用同一套条件（含 ``isinstance`` 与 strip 过滤），
    否则会出现「标签说有技能、渲染却没输出技能」的两套真源。
    """
    labels: List[str] = []
    if s.get("summary"):
        labels.append("个人总结")
    education = s.get("education") if isinstance(s.get("education"), list) else []
    if education:
        labels.append("教育经历")
    experiences = s.get("experiences") if isinstance(s.get("experiences"), list) else []
    if experiences:
        for exp in experiences:
            if not isinstance(exp, dict):
                continue
            labels.append(_TYPE_SECTION.get(str(exp.get("type") or "project"), "经历"))
    if [x for x in (s.get("skills") or []) if str(x).strip()]:
        labels.append("技能")
    if [x for x in (s.get("certifications") or []) if str(x).strip()]:
        labels.append("证书")
    return labels


def _two_column_table(main: List, side: List, main_w: float, side_w: float):
    """把两栏各自的 flowable 列表并排放进一个无边框表格。

    为什么用「表格里塞 flowable 列表」而不是双 Frame：一页自适应那套逻辑
    （``_measure_story`` 量自然高度 → 调间距/行距 → 渲染测页数）全部是按
    「一连串 flowable 的总高度」写的。表格本身就是一个 flowable，它的高度
    已经由 reportlab 按 `max(主栏, 副栏)` 算好，整条链路**不用改**。
    若改用两个 Frame，就得重写度量与页数判断——那才是真正的返工。

    列宽按列宽和 = 版心宽度：主栏格子额外吃掉一个栏间距，右内边距也设成
    栏间距，于是两栏内容之间正好留下 gutter、右侧不留多余空白。
    """
    tbl = Table([[main, side]], colWidths=[main_w + _COLUMN_GUTTER_PT, side_w],
                hAlign="LEFT")
    tbl.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (0, 0), _COLUMN_GUTTER_PT),
        ("RIGHTPADDING", (1, 0), (1, 0), 0),
        ("TOPPADDING", (0, 0), (-1, -1), 0),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
    ]))
    return tbl


# ============ 结构化渲染（简历生成模块，保留层次） ============

_TYPE_SECTION = {"project": "项目经历", "internship": "实习经历", "work": "工作经历"}


def _generation_structured(resume: Resume, override: Optional[dict] = None) -> Optional[dict]:
    """从简历的 parsed_json 提取结构化数据（生成版的 generation 或优化版的 structured）。

    ``override`` 供「导出前惰性提取」使用：那份结构化数据只存在于内存中
    （导出是只读动作，不回写数据库，见 `services.ensure_resume_structured`）。
    """
    if isinstance(override, dict) and override.get("name"):
        return override
    pj = resume.parsed_json or {}
    gen = pj.get("generation") or pj.get("structured")
    if isinstance(gen, dict) and gen.get("name"):
        return gen
    return None


def source_photo_bytes(resume: Resume) -> Optional[bytes]:
    """读取上传 DOCX 时保存的头像；损坏或缺失时静默回退到无头像布局。"""
    payload = resume.parsed_json if isinstance(resume.parsed_json, dict) else {}
    photo = payload.get("source_photo")
    if not isinstance(photo, dict):
        return None
    encoded = photo.get("data")
    if not isinstance(encoded, str) or not encoded:
        return None
    try:
        data = base64.b64decode(encoded, validate=True)
    except (ValueError, TypeError):
        return None
    return data or None


def _pdf_photo_flowable(photo: bytes, *, max_width: float, max_height: float):
    """保持头像宽高比，按页头可用空间缩放。"""
    try:
        reader = ImageReader(io.BytesIO(photo))
        width, height = reader.getSize()
    except Exception:
        return None
    if width <= 0 or height <= 0:
        return None
    scale = min(max_width / width, max_height / height)
    return RLImage(
        io.BytesIO(photo),
        width=width * scale,
        height=height * scale,
    )


def _pdf_header_table(left: List, photo, content_width_pt: float) -> List:
    """把姓名/联系方式与头像放进无边框两列表格。"""
    if photo is None:
        return left
    gap = 7 * mm
    left_width = max(content_width_pt - photo.drawWidth - gap, 1)
    table = Table(
        [[left, photo]],
        colWidths=[left_width, photo.drawWidth + gap],
        hAlign="LEFT",
    )
    table.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("ALIGN", (1, 0), (1, 0), "RIGHT"),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ("TOPPADDING", (0, 0), (-1, -1), 0),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
    ]))
    return [table]


def _span(start, end) -> str:
    """时间区间："2023 – 2026"（用 en dash，全字体齐备）。"""
    s = str(start or "").strip()
    e = str(end or "").strip()
    if s and e:
        return f"{s} {EN_DASH} {e}"
    return s or e


def _join_meta(*parts) -> str:
    """用 · 连接非空字段。"""
    return MIDDOT.join(str(p).strip() for p in parts if p and str(p).strip())


def _edu_detail(edu: dict) -> str:
    """教育经历子行：主修课程 · 绩点 · 荣誉。"""
    if edu.get("detail"):
        return str(edu["detail"]).strip()
    return _join_meta(
        f"主修课程：{edu['courses']}" if edu.get("courses") else "",
        f"绩点：{edu['gpa']}" if edu.get("gpa") else "",
        f"荣誉：{edu['honors']}" if edu.get("honors") else "",
    )


def _pdf_story_structured(
    font: str,
    s: dict,
    body: float,
    leading: float,
    heading: float,
    contact_size: float,
    title_size: float,
    margin: float,
    extra_gap: float = 0.0,
    lead_scale: float = 1.0,
    photo: Optional[bytes] = None,
    sidebar_sections: Tuple[str, ...] = (),
    sidebar_ratio: float = _DEFAULT_SIDEBAR_RATIO,
) -> Tuple[List, int]:
    """构建结构化简历故事流，返回 (story, 模块数)。

    页头（姓名/意向/联系方式）始终横跨全宽；正文按 ``sidebar_sections`` 分成
    主栏/副栏两块，最后并排拼成一个表格。``sidebar_sections`` 为空时副栏不存在，
    整条流程与改造前的单栏逐字节一致。
    """
    st = _styles(font, body, leading, heading, contact_size, title_size, extra_gap, lead_scale)
    content_width_mm = 210 - 2 * margin
    content_width_pt = content_width_mm * mm

    # 先证明这份简历里真有可放副栏的分区，再决定分不分栏（否则主栏白白被压窄）
    sidebar_sections = _effective_sidebar(sidebar_sections, _structured_section_labels(s))
    main: List = []
    # side 用 None / 列表区分「不开双栏」与「双栏但暂时还没内容」：
    # 若用空列表当哨兵，`if side` 在 append 之前恒为假，副栏永远进不去。
    side: Optional[List] = [] if sidebar_sections else None
    main_w, side_w = _column_widths(content_width_pt, sidebar_sections, sidebar_ratio)

    # 当前写入的列 + 该列的可用宽度（分栏后是栏宽，不是版心宽）
    cur: List[List] = [main]
    cur_w: List[float] = [main_w]

    story: List = []
    blocks = [0]

    def _enter(label: str) -> None:
        """切换当前栏：声明为副栏的分区进副栏，其余一律回主栏。"""
        into_side = side is not None and _goes_sidebar(label, sidebar_sections)
        cur[0] = side if into_side else main
        cur_w[0] = side_w if into_side else main_w

    def _push(title: str):
        """插入模块标题（含间距）。blocks[0] 用于自适应时按模块数分配留白。"""
        col = cur[0]
        col.extend(_heading_flowable(title, st, cur_w[0], heading,
                                     first=len(col) == 0))
        blocks[0] += 1

    def _out(*flowables) -> None:
        cur[0].extend(flowables)

    def _row(left_html: str, right_html: str):
        """条目标题行：宽度跟着**当前栏**走，不能拿版心宽去算。"""
        return _exp_header_row(left_html, right_html, st["item_title"], st["time"],
                               cur_w[0] / mm)

    # 姓名 / 求职意向 / 联系方式；有原始头像时放右侧，保持原简历的视觉锚点。
    header: List = []
    if s.get("name"):
        header.append(Paragraph(_bold(str(s["name"])), st["name"]))

    # 求职意向单独一行（主色突出），联系方式单独一行（弱化）
    contact = s.get("contact") if isinstance(s.get("contact"), dict) else {}
    if s.get("position"):
        header.append(Paragraph(_bold(f"求职意向：{s['position']}"), st["position"]))
    contact_parts = []
    if contact.get("phone"):
        contact_parts.append(f"电话：{contact['phone']}")
    if contact.get("email"):
        contact_parts.append(f"邮箱：{contact['email']}")
    if contact.get("github"):
        contact_parts.append(f"GitHub：{contact['github']}")
    if contact_parts:
        header.append(Paragraph(_escape("  |  ".join(contact_parts)), st["contact"]))

    photo_flowable = None
    if photo:
        photo_flowable = _pdf_photo_flowable(
            photo, max_width=28 * mm, max_height=38 * mm,
        )
    story.extend(_pdf_header_table(header, photo_flowable, content_width_pt))

    # 页头分隔线：细主色线，比原来的纯黑粗线克制
    story.append(HRFlowable(width="100%", thickness=1.0, color=_PRIMARY,
                            spaceBefore=1, spaceAfter=0))

    # 个人总结
    if s.get("summary"):
        _enter("个人总结")
        _push("个人总结")
        _out(Paragraph(_escape(str(s["summary"])), st["summary"]))

    # 教育经历
    education = s.get("education") if isinstance(s.get("education"), list) else []
    if education:
        _enter("教育经历")
        _push("教育经历")
        for edu in education:
            if not isinstance(edu, dict):
                continue
            school_major = " ".join(
                str(x).strip()
                for x in (edu.get("school"), edu.get("major"), edu.get("degree"))
                if x and str(x).strip()
            )
            span = _span(edu.get("start"), edu.get("end"))
            if school_major:
                left = _bold(school_major)
                if span:
                    _out(_row(left, f'<font color="#64748B">{_escape(span)}</font>'))
                else:
                    _out(Paragraph(left, st["item_title"]))
            detail = _edu_detail(edu)
            if detail:
                _out(Paragraph(_escape(detail), st["detail"]))

    # 经历（按类型分组：项目 / 实习 / 工作）
    experiences = s.get("experiences") if isinstance(s.get("experiences"), list) else []
    if experiences:
        groups: dict = {}
        for exp in experiences:
            if not isinstance(exp, dict):
                continue
            t = str(exp.get("type") or "project")
            groups.setdefault(_TYPE_SECTION.get(t, "经历"), []).append(exp)
        for title, items in groups.items():
            _enter(title)
            _push(title)
            for exp in items:
                name = str(exp.get("name") or "").strip()
                role = str(exp.get("role") or "").strip()
                span = _span(exp.get("start"), exp.get("end"))
                if name:
                    left = _bold(name)
                    if role:
                        left += f' <font color="#64748B">{_escape(role)}</font>'
                    if span:
                        _out(_row(left, f'<font color="#64748B">{_escape(span)}</font>'))
                    else:
                        _out(Paragraph(left, st["item_title"]))
                bullets = exp.get("bullets") if isinstance(exp.get("bullets"), list) else []
                for b in bullets:
                    text = str(b).strip()
                    if text:
                        _out(Paragraph(
                            _highlight_quantities(_escape(text)), st["bullet"], bulletText=BULLET))

    # 技能
    skills = [str(x).strip() for x in (s.get("skills") or []) if str(x).strip()]
    if skills:
        _enter("技能")
        _push("技能")
        _out(Paragraph(_escape(MIDDOT.join(skills)), st["plain"]))

    # 证书
    certs = [str(x).strip() for x in (s.get("certifications") or []) if str(x).strip()]
    if certs:
        _enter("证书")
        _push("证书")
        _out(Paragraph(_escape(MIDDOT.join(certs)), st["plain"]))

    # 两栏并排；单栏时把主栏原样接回 story，与改造前完全等价。
    if side:
        story.append(_two_column_table(main, side, main_w, side_w))
    else:
        story.extend(main)

    return story, blocks[0]


def _pdf_story_plain(
    font: str,
    structured: StructuredResume,
    body: float,
    leading: float,
    heading: float,
    contact_size: float,
    title_size: float,
    margin: float,
    extra_gap: float = 0.0,
    lead_scale: float = 1.0,
    photo: Optional[bytes] = None,
    sidebar_sections: Tuple[str, ...] = (),
    sidebar_ratio: float = _DEFAULT_SIDEBAR_RATIO,
) -> Tuple[List, int]:
    """纯文本兜底路径：走同一套样式，保证与结构化导出观感一致。

    分区标题是从用户文档里原样抠出来的（可能是「技能特长」这类变体），
    所以进副栏与否走关键词包含判断（``_goes_sidebar``），而不是等值匹配。
    """
    st = _styles(font, body, leading, heading, contact_size, title_size, extra_gap, lead_scale)
    content_width_pt = (210 - 2 * margin) * mm

    # 同结构化路径：没有真实存在的副栏分区就不分栏
    sidebar_sections = _effective_sidebar(
        sidebar_sections, [section.title for section in structured.sections])
    main: List = []
    side: Optional[List] = [] if sidebar_sections else None
    main_w, side_w = _column_widths(content_width_pt, sidebar_sections, sidebar_ratio)

    story: List = []
    blocks = 0
    header: List = []
    if structured.name:
        header.append(Paragraph(_bold(structured.name), st["name"]))
    if structured.contact:
        header.append(Paragraph(_escape("  |  ".join(structured.contact)), st["contact"]))

    photo_flowable = None
    if photo:
        photo_flowable = _pdf_photo_flowable(
            photo, max_width=28 * mm, max_height=38 * mm,
        )
    story.extend(_pdf_header_table(header, photo_flowable, content_width_pt))

    story.append(HRFlowable(width="100%", thickness=1.0, color=_PRIMARY,
                            spaceBefore=1, spaceAfter=0))

    for section in structured.sections:
        into_side = side is not None and _goes_sidebar(section.title, sidebar_sections)
        col = side if into_side else main
        col_w = side_w if into_side else main_w
        col.extend(_heading_flowable(section.title, st, col_w, heading,
                                     first=len(col) == 0))
        blocks += 1
        for line in _split_bullets(section.lines):
            text = _bullet_line(line)
            if not text:
                continue
            col.append(Paragraph(
                _highlight_quantities(_escape(text)), st["bullet"], bulletText=BULLET))

    if side:
        story.append(_two_column_table(main, side, main_w, side_w))
    else:
        story.extend(main)

    return story, blocks


# ============ 渲染与自适应 ============

def _measure_story(story: List, avail_w: float, avail_h: float) -> float:
    """逐条 wrap 出自然高度，用于判断内容偏少时该补多少留白。"""
    total = 0.0
    for flowable in story:
        try:
            _w, h = flowable.wrap(avail_w, avail_h)
        except Exception:
            continue
        total += h
        for attr in ("getSpaceBefore", "getSpaceAfter"):
            getter = getattr(flowable, attr, None)
            if callable(getter):
                try:
                    total += getter()
                except Exception:
                    pass
    return total


def _render(story: List, margin: float, title: str = "简历") -> Tuple[bytes, int]:
    """渲染 story 并返回 (PDF 字节, 页数)。

    **必须传副本给 ``doc.build``**：reportlab 在 build 过程中会把传入的列表
    逐个 ``pop`` 掉（渲完 `story` 就成了空列表）。而 ``_adaptive_pdf`` 里有一条
    「舒展过头 → 回退重渲 base_story」的兜底分支，它假定原 story 还在原地；
    一旦被吃空，那条分支会渲出一个 **0 页的空白 PDF** 并被当作成功返回
    （实测：空白产物仅 932 字节）。传 `list(story)` 让渲染变成只读动作。
    """
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        leftMargin=margin * mm,
        rightMargin=margin * mm,
        topMargin=_TOP_MARGIN_MM * mm,
        bottomMargin=_BOTTOM_MARGIN_MM * mm,
        title=title,
        author="OfferFlow",
        canvasmaker=_EmbossCanvas,
    )
    doc.build(list(story))
    return buffer.getvalue(), doc.page


def _render_tier(story: List, margin: float) -> Optional[Tuple[bytes, int]]:
    """渲染一档；双栏容器整行塞不下会抛 ``LayoutError``，这里返回 None 表示
    「这一档放不下」，由调用方降档重试。

    为什么不能在第一次抛异常时就整份退回单栏：双栏表格的行是**原子**的
    （不能跨页拆），内容稍长时最大字号那一档必抛；但它往往在降一两档之后就
    正好装进一页。若在这里直接放弃，用户点了「双栏」却几乎总是拿到单栏。
    """
    try:
        return _render(story, margin)
    except LayoutError:
        logger.info("导出排版：当前档位放不下（多为双栏整行超页），降档重试")
        return None


def _adaptive_pdf(build_story) -> bytes:
    """一页自适应：逐档尝试字号；能装下时再按剩余空间把版面铺满。

    build_story(tier, extra_gap, lead_scale) -> (story, blocks)
    """
    last_data: Optional[bytes] = None
    for tier in _SIZE_TIERS:
        margin = tier[5]
        avail_w = A4[0] - 2 * margin * mm
        avail_h = A4[1] - (_TOP_MARGIN_MM + _BOTTOM_MARGIN_MM) * mm

        base_story, blocks = build_story(tier, 0.0, 1.0)
        natural = _measure_story(base_story, avail_w, avail_h)
        if natural > avail_h:
            # 这一档本身就塞不下，先渲染留作兜底，再降档
            rendered = _render_tier(base_story, margin)
            if rendered is None:
                continue
            data, pages = rendered
            last_data = data
            if pages <= 1:
                return data
            continue

        # 内容偏少：先补模块间距，还不够再适度放开行距，避免空白全堆在页脚。
        # 内容越少，允许的模块间距越疏朗（疏朗是设计感，堆在顶部才是难看）。
        relax = max(1.0 - min((natural / avail_h) / _GAP_RELAX_AT, 1.0), 0.0)
        max_gap = min(_MAX_MODULE_GAP + relax * _MAX_MODULE_GAP_EXTRA, 34.0)

        target = avail_h * _TARGET_FILL
        budget = max(target - natural, 0.0)
        extra_gap = min(budget / blocks, max_gap) if blocks else 0.0
        lead_scale = 1.0
        if extra_gap > 0.5:
            filled = _measure_story(build_story(tier, extra_gap, 1.0)[0], avail_w, avail_h)
            # 行距只放大「文字」那一部分，Spacer 间距不随行距变化，先把它刨掉再算
            gap_total = extra_gap * blocks
            text_h = max(filled - gap_total, 1.0)
            need = max(target - gap_total, 0.0)
            lead_scale = min(max(need / text_h, 1.0), _MAX_LEAD_SCALE)

        story = base_story
        if extra_gap > 0.5 or lead_scale > 1.001:
            story, _ = build_story(tier, extra_gap, lead_scale)

        # 内容仍然明显偏空时，把余量的一部分补到页头之上，
        # 让内容在页面里大致居中，而不是整块贴在顶部、底下留一大片。
        filled_h = _measure_story(story, avail_w, avail_h)
        slack = avail_h - filled_h
        if slack > _TOP_PAD_MIN_SLACK:
            top_pad = min((slack - _TOP_PAD_MIN_SLACK) * _TOP_PAD_RATIO, _TOP_PAD_CAP)
            if top_pad > 1.0:
                story = [Spacer(1, top_pad)] + story

        rendered = _render_tier(story, margin)
        if rendered is None:
            continue
        data, pages = rendered
        if pages > 1:
            # 舒展过了头：回退到本档紧凑版，宁可留白也不溢出到第二页
            compact = _render_tier(base_story, margin)
            if compact is None:
                continue
            data, pages = compact
        last_data = data
        if pages <= 1:
            return data

    return last_data if last_data is not None else b""


def _adaptive_pdf_or_single(build_story, fallback_story=None) -> bytes:
    """跑一页自适应；**所有档位**都排不下双栏时，整份退回单栏重跑。

    注意分工：单档放不下由 ``_render_tier`` 负责降档重试（见其注释）；
    这里兜的是「降到最后仍装不进一页」——reportlab 的表格整行不能跨页拆，
    双栏容器只有一行，装不下就是硬失败。单栏是「一串离散 flowable」，
    天然能溢出到第 2 页再由降档收回来，所以它是这张网的最后一层。

    判据不能只看「有没有抛异常」：``_render_tier`` 已经把 LayoutError 转成
    「这档放不下」，全档失败时 ``_adaptive_pdf`` 是**安静地返回空字节**，
    所以「产物为空」同样是必须触发兜底的信号（否则接口会返回 0 字节文件）。
    """
    data = b""
    try:
        data = _adaptive_pdf(build_story)
    except LayoutError:
        data = b""
    if data or fallback_story is None:
        return data
    logger.warning("双栏版式在所有档位都排不下，退回单栏重排")
    return _adaptive_pdf(fallback_story)


def export_to_pdf(
    resume: Resume,
    structured: Optional[dict] = None,
    source_photo: Optional[bytes] = None,
    layout: Optional[str] = None,
) -> bytes:
    """导出简历为 PDF，自动压缩到一页。

    简历生成模块的结果（parsed_json.generation）用结构化渲染，精确保留层次
    （姓名/项目名加粗、bullet 圆点、教育子行缩进），其余走纯文本解析兜底。

    ``structured``：导出前惰性提取出来的结构化数据（内存态，不写库）。
    ``layout``：``"single"``（默认）或 ``"split"``（双栏：主栏经历 / 副栏技能证书）。
    未知取值回退单栏，不抛异常。这份简历压根没有技能/证书分区时会自然地退回
    单栏——那是「没东西可放」，不是降级失败；而内容过高导致双栏排不下时，
    会明确退回单栏重排（见 ``_adaptive_pdf_or_single``）。
    """
    font = _register_font()
    resolved = _generation_structured(resume, structured)
    photo = source_photo or source_photo_bytes(resume)
    _, sidebar_sections = resolve_layout(layout)

    def _builder_structured(sections: Tuple[str, ...]):
        return lambda tier, gap, ls: _pdf_story_structured(
            font, resolved, tier[0], tier[1], tier[2], tier[3], tier[4], tier[5],
            gap, ls, photo, sections, _DEFAULT_SIDEBAR_RATIO)

    def _builder_plain(parsed: StructuredResume, sections: Tuple[str, ...]):
        return lambda tier, gap, ls: _pdf_story_plain(
            font, parsed, tier[0], tier[1], tier[2], tier[3], tier[4], tier[5],
            gap, ls, photo, sections, _DEFAULT_SIDEBAR_RATIO)

    if resolved:
        primary = _builder_structured(sidebar_sections)
        if sidebar_sections:
            return _adaptive_pdf_or_single(primary, _builder_structured(()))
        return _adaptive_pdf(primary)

    parsed = parse_resume_structure(resume.raw_text)
    primary = _builder_plain(parsed, sidebar_sections)
    if sidebar_sections:
        return _adaptive_pdf_or_single(primary, _builder_plain(parsed, ()))
    return _adaptive_pdf(primary)


# ============ Word ============

def _estimate_word_size(char_count: int) -> float:
    """根据字符数估算 Word 正文字号（保留给外部调用的兼容入口）。"""
    return _word_layout(char_count)[0]


def _word_layout(char_count: int) -> Tuple[float, float, float]:
    """按内容量选 Word 的 (正文字号, 左右边距cm, 间距系数)。

    Word 端没法像 PDF 那样「渲染后测页数再回退」，只能靠字符量保守估。
    三项一起收：内容越多 → 字越小、版心越宽、段落间距越紧，才能压进一页。
    间距系数同时作用于模块标题的段前距与项目符号的段后距。
    """
    for threshold, size, margin_cm, gap_scale in [
        (560, 10.5, 1.9, 1.00),
        (760, 10.0, 1.8, 0.85),
        (920, 9.5, 1.7, 0.70),
        (1080, 9.0, 1.6, 0.55),
        (1160, 8.5, 1.5, 0.40),
    ]:
        if char_count <= threshold:
            return size, margin_cm, gap_scale
    return 8.0, 1.4, 0.35


def _structured_char_count(s: dict) -> int:
    """统计结构化简历真实会写进文档的字符量。

    不能拿 resume.raw_text 来估——「生成版」简历的 raw_text 常常很短甚至为空，
    而真正要排版的是 parsed_json.generation 里的内容，用 raw_text 会严重低估、
    导致选到过大的字号、Word 溢出到第二页。
    """
    parts: List[str] = [str(s.get("name") or ""), str(s.get("position") or "")]
    contact = s.get("contact") if isinstance(s.get("contact"), dict) else {}
    parts.extend(str(v or "") for v in contact.values())
    parts.append(str(s.get("summary") or ""))
    for edu in (s.get("education") or []):
        if isinstance(edu, dict):
            parts.extend(str(edu.get(k) or "") for k in
                         ("school", "major", "degree", "courses", "gpa", "honors", "detail"))
    for exp in (s.get("experiences") or []):
        if isinstance(exp, dict):
            parts.append(str(exp.get("name") or ""))
            parts.append(str(exp.get("role") or ""))
            parts.extend(str(b) for b in (exp.get("bullets") or []))
    parts.extend(str(x) for x in (s.get("skills") or []))
    parts.extend(str(x) for x in (s.get("certifications") or []))
    return sum(len(p) for p in parts)


def _word_doc_setup(doc, body_size: float, margin_cm: float = 1.8):
    """统一的 Word 页面与段落基线。

    **关键**：python-docx 的默认模板里 Normal 样式自带 `段后 8pt + 1.08 倍行距`。
    不把它压掉的话，一份简历几十个段落会累计出大半页的额外空白，
    内容稍多就必然溢到第二页。这里清零基线，再由各段自行设置需要的间距。

    返回内容区宽度（cm），用于右对齐时间的制表位。
    """
    style = doc.styles["Normal"]
    style.font.name = "微软雅黑"
    style.font.size = Pt(body_size)
    pf = style.paragraph_format
    pf.space_before = Pt(0)
    pf.space_after = Pt(0)
    pf.line_spacing = 1.0

    section = doc.sections[0]
    # python-docx 默认模板是 Letter；导出必须显式落成 A4，否则 Word/WPS
    # 的可用高度不同，一页约束和底部留白都会出现跨软件漂移。
    section.page_width = Cm(21.0)
    section.page_height = Cm(29.7)
    section.top_margin = Cm(1.4)
    section.bottom_margin = Cm(1.4)
    section.left_margin = Cm(margin_cm)
    section.right_margin = Cm(margin_cm)
    _word_fill_page(section)
    return 21.0 - 2 * margin_cm


def _word_fill_page(section) -> None:
    """让 Word 垂直分布段落，消除内容已在一页内但底部大片留白。"""
    sect_pr = section._sectPr
    v_align = sect_pr.find(qn("w:vAlign"))
    if v_align is None:
        v_align = OxmlElement("w:vAlign")
        doc_grid = sect_pr.find(qn("w:docGrid"))
        if doc_grid is not None:
            doc_grid.addprevious(v_align)
        else:
            sect_pr.append(v_align)
    v_align.set(qn("w:val"), "both")


def _word_set_font(run, size: float, bold: bool = False, color=None):
    """统一设置中文字体（含 eastAsia），否则 Word 里中文会退回宋体。"""
    run.font.name = "微软雅黑"
    run.font.size = Pt(size)
    run.font.bold = bold
    if color is not None:
        run.font.color.rgb = color
    rPr = run._element.get_or_add_rPr()
    rFonts = rPr.find(qn("w:rFonts"))
    if rFonts is None:
        rFonts = OxmlElement("w:rFonts")
        rPr.append(rFonts)
    rFonts.set(qn("w:eastAsia"), "微软雅黑")


def _word_heading_border(p):
    """给模块标题段落加浅灰下边框。"""
    pPr = p._p.get_or_add_pPr()
    pBdr = OxmlElement("w:pBdr")
    bottom = OxmlElement("w:bottom")
    bottom.set(qn("w:val"), "single")
    bottom.set(qn("w:sz"), "6")
    bottom.set(qn("w:space"), "3")
    bottom.set(qn("w:color"), "CBD5E1")
    pBdr.append(bottom)
    pPr.append(pBdr)


def _word_runs_highlight(p, text: str, size: float, base_color, accent_color):
    """把一段文字按量化数字拆成多个 run：数字用强调色，其余用正文色。

    与 PDF 侧的 _highlight_quantities 对应，保证两种导出格式的
    「成果重点」高亮表现一致。
    """
    pos = 0
    for match in _QUANT_PATTERN.finditer(text):
        if match.start() > pos:
            _word_set_font(p.add_run(text[pos:match.start()]), size, color=base_color)
        _word_set_font(p.add_run(match.group(0)), size, color=accent_color)
        pos = match.end()
    if pos < len(text):
        _word_set_font(p.add_run(text[pos:]), size, color=base_color)


def _word_bullet(doc, text: str, size: float, gap_scale: float = 1.0):
    """项目符号：手动加 ● 前缀 + 悬挂缩进（不依赖 Word 的 List Bullet 编号定义，
    WPS / Word 表现一致，颜色也能控）。"""
    p = doc.add_paragraph()
    pf = p.paragraph_format
    pf.left_indent = Pt(size * 1.3)
    pf.first_line_indent = Pt(-size * 1.15)
    pf.space_after = Pt(1.5 * gap_scale)
    _word_set_font(p.add_run(f"{BULLET} "), size * 0.62, color=RGBColor(0x1E, 0x3A, 0x8A))
    _word_runs_highlight(p, text, size, RGBColor(0x33, 0x41, 0x55),
                         RGBColor(0x1E, 0x3A, 0x8A))
    return p


def _word_header_block(
    doc,
    *,
    name: str,
    position: str,
    contact_line: str,
    photo: Optional[bytes],
    name_size: float,
    body_size: float,
    ink,
    primary,
    muted,
    content_width_cm: float,
) -> None:
    """写页头；有原始头像时使用两列表格，避免姓名/联系方式被覆盖。"""
    if not photo:
        if name:
            p = doc.add_paragraph()
            p.alignment = WD_ALIGN_PARAGRAPH.LEFT
            p.paragraph_format.space_after = Pt(2)
            _word_set_font(p.add_run(name), name_size, bold=True, color=ink)
        if position:
            p = doc.add_paragraph()
            p.paragraph_format.space_after = Pt(1)
            _word_set_font(p.add_run(f"求职意向：{position}"), body_size + 1.5,
                           bold=True, color=primary)
        if contact_line:
            p = doc.add_paragraph()
            p.paragraph_format.space_after = Pt(2)
            _word_set_font(p.add_run(contact_line), body_size - 1, color=muted)
        return

    photo_width_cm = 3.0
    table = doc.add_table(rows=1, cols=2)
    table.autofit = False
    left, right = table.rows[0].cells
    left.width = Cm(max(content_width_cm - photo_width_cm, 1))
    right.width = Cm(photo_width_cm)
    table.columns[0].width = left.width
    table.columns[1].width = right.width

    left_p = left.paragraphs[0]
    if name:
        _word_set_font(left_p.add_run(name), name_size, bold=True, color=ink)
    if position:
        p = left.add_paragraph()
        p.paragraph_format.space_after = Pt(1)
        _word_set_font(p.add_run(f"求职意向：{position}"), body_size + 1.5,
                       bold=True, color=primary)
    if contact_line:
        p = left.add_paragraph()
        p.paragraph_format.space_after = Pt(2)
        _word_set_font(p.add_run(contact_line), body_size - 1, color=muted)

    right_p = right.paragraphs[0]
    right_p.alignment = WD_ALIGN_PARAGRAPH.RIGHT
    right_p.paragraph_format.space_after = Pt(0)
    right_p.add_run().add_picture(io.BytesIO(photo), width=Cm(photo_width_cm))


def export_to_word(
    resume: Resume,
    structured: Optional[dict] = None,
    source_photo: Optional[bytes] = None,
) -> bytes:
    """导出简历为 Word（结构化结果优先，保留层次）。

    ``structured``：导出前惰性提取出来的结构化数据（内存态，不写库）。
    """
    photo = source_photo or source_photo_bytes(resume)
    resolved = _generation_structured(resume, structured)
    if resolved:
        return _export_word_structured(resume, resolved, photo)

    parsed = parse_resume_structure(resume.raw_text)
    body_size, margin_cm, gap_scale = _word_layout(len(resume.raw_text or ""))
    heading_size = body_size + 1.5
    name_size = body_size + 5

    doc = Document()
    content_width_cm = _word_doc_setup(doc, body_size, margin_cm)

    _word_header_block(
        doc,
        name=parsed.name or "",
        position="",
        contact_line="  |  ".join(parsed.contact or []),
        photo=photo,
        name_size=name_size,
        body_size=body_size,
        ink=RGBColor(0x0F, 0x17, 0x2A),
        primary=RGBColor(0x1E, 0x3A, 0x8A),
        muted=RGBColor(0x64, 0x74, 0x8B),
        content_width_cm=content_width_cm,
    )

    for section in parsed.sections:
        hp = doc.add_paragraph()
        hp.paragraph_format.space_before = Pt(9 * gap_scale)
        hp.paragraph_format.space_after = Pt(2 * gap_scale)
        _word_set_font(hp.add_run(section.title), heading_size, bold=True,
                       color=RGBColor(0x1E, 0x3A, 0x8A))
        _word_heading_border(hp)
        for line in _split_bullets(section.lines):
            text = _bullet_line(line)
            if text:
                _word_bullet(doc, text, body_size, gap_scale)

    buffer = io.BytesIO()
    doc.save(buffer)
    return buffer.getvalue()


def _export_word_structured(
    resume: Resume,
    s: dict,
    photo: Optional[bytes] = None,
) -> bytes:
    """简历生成结果的结构化 Word 导出（保留层次）。"""
    body_size, margin_cm, gap_scale = _word_layout(_structured_char_count(s))
    heading_size = body_size + 1.5
    name_size = body_size + 5

    ink = RGBColor(0x0F, 0x17, 0x2A)
    primary = RGBColor(0x1E, 0x3A, 0x8A)
    text_color = RGBColor(0x33, 0x41, 0x55)
    muted = RGBColor(0x64, 0x74, 0x8B)

    doc = Document()
    content_width_cm = _word_doc_setup(doc, body_size, margin_cm)

    def _para(tab_right: bool = False, space_after: float = 1.0):
        p = doc.add_paragraph()
        pf = p.paragraph_format
        pf.space_after = Pt(space_after * gap_scale)
        if tab_right:
            pf.tab_stops.add_tab_stop(Cm(content_width_cm), WD_TAB_ALIGNMENT.RIGHT)
        return p

    def _heading(title: str):
        hp = doc.add_paragraph()
        hp.paragraph_format.space_before = Pt(9 * gap_scale)
        hp.paragraph_format.space_after = Pt(2 * gap_scale)
        _word_set_font(hp.add_run(title), heading_size, bold=True, color=primary)
        _word_heading_border(hp)

    # 姓名 / 求职意向 / 联系方式；有原始头像时放右侧。
    contact = s.get("contact") if isinstance(s.get("contact"), dict) else {}
    parts = []
    if contact.get("phone"):
        parts.append(f"电话：{contact['phone']}")
    if contact.get("email"):
        parts.append(f"邮箱：{contact['email']}")
    if contact.get("github"):
        parts.append(f"GitHub：{contact['github']}")
    _word_header_block(
        doc,
        name=str(s.get("name") or ""),
        position=str(s.get("position") or ""),
        contact_line="  |  ".join(parts),
        photo=photo,
        name_size=name_size,
        body_size=body_size,
        ink=ink,
        primary=primary,
        muted=muted,
        content_width_cm=content_width_cm,
    )

    # 个人总结
    if s.get("summary"):
        _heading("个人总结")
        p = _para(space_after=1.5)
        _word_runs_highlight(p, str(s["summary"]), body_size, text_color, primary)

    # 教育经历
    education = s.get("education") if isinstance(s.get("education"), list) else []
    if education:
        _heading("教育经历")
        for edu in education:
            if not isinstance(edu, dict):
                continue
            school_major = " ".join(
                str(x).strip()
                for x in (edu.get("school"), edu.get("major"), edu.get("degree"))
                if x and str(x).strip()
            )
            span = _span(edu.get("start"), edu.get("end"))
            if school_major:
                p = _para(tab_right=True, space_after=0.5)
                _word_set_font(p.add_run(school_major), body_size + 0.5, bold=True, color=ink)
                if span:
                    _word_set_font(p.add_run("\t" + span), body_size - 0.5, color=muted)
            detail = _edu_detail(edu)
            if detail:
                p = _para(space_after=0.5)
                p.paragraph_format.left_indent = Pt(body_size * 0.8)
                _word_set_font(p.add_run(detail), body_size - 0.5, color=muted)

    # 经历（按类型分组）
    experiences = s.get("experiences") if isinstance(s.get("experiences"), list) else []
    if experiences:
        groups: dict = {}
        for exp in experiences:
            if not isinstance(exp, dict):
                continue
            t = str(exp.get("type") or "project")
            groups.setdefault(_TYPE_SECTION.get(t, "经历"), []).append(exp)
        for title, items in groups.items():
            _heading(title)
            for exp in items:
                name = str(exp.get("name") or "").strip()
                role = str(exp.get("role") or "").strip()
                span = _span(exp.get("start"), exp.get("end"))
                if name:
                    p = _para(tab_right=True, space_after=0.5)
                    _word_set_font(p.add_run(name), body_size + 0.5, bold=True, color=ink)
                    if role:
                        _word_set_font(p.add_run("  " + role), body_size - 0.5, color=muted)
                    if span:
                        _word_set_font(p.add_run("\t" + span), body_size - 0.5, color=muted)
                bullets = exp.get("bullets") if isinstance(exp.get("bullets"), list) else []
                for b in bullets:
                    text = str(b).strip()
                    if text:
                        _word_bullet(doc, text, body_size, gap_scale)

    # 技能
    skills = [str(x).strip() for x in (s.get("skills") or []) if str(x).strip()]
    if skills:
        _heading("技能")
        p = _para(space_after=1.5)
        _word_set_font(p.add_run(MIDDOT.join(skills)), body_size, color=text_color)

    # 证书
    certs = [str(x).strip() for x in (s.get("certifications") or []) if str(x).strip()]
    if certs:
        _heading("证书")
        p = _para(space_after=1.5)
        _word_set_font(p.add_run(MIDDOT.join(certs)), body_size, color=text_color)

    buffer = io.BytesIO()
    doc.save(buffer)
    return buffer.getvalue()
