"""Word 导出：多模板版式引擎。

版式参数全部来自 services/templates.py，与 PDF 导出共用同一套定义——
这样「选了什么模板」在 docx 和 PDF 里是一致的。
"""

import copy
import math
import os
import re
from typing import Any, Dict, List, Optional, Tuple

from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_TAB_ALIGNMENT
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Cm, Pt, RGBColor

from ..core.resume_review import from_structured, parse_resume_text
from ..core.sections import SECTION_LABELS, normalize_section_order
from . import templates

# ============ 版式常量 ============

PAGE_W_CM, PAGE_H_CM = 21.0, 29.7

# 实测结论（纯中文段落、A4、Word 转 PDF 后量相邻两行基线间距）：
# Word 的「N 倍行距」= N × 字体自带行高，与字号无关——同一个 10.5pt 字号，
# 微软雅黑单倍行距 18.0pt，宋体/黑体 13.6pt，等线 14.2pt。
# 而桌面版 PDF 引擎的「N 倍行距」= N × 字号。同一个模板下两者能差 30%~45%，
# 于是同一份简历会出现「PDF 一页、Word 两页」。修法是把 Word 侧统一切成
# 「精确行距」= 字号 × 倍数，让两套引擎口径一致（见 _body_line_pt / _para）。
_HEAD_LINE_FACTOR = 1.35     # 页头/小节标题的精确行距（相对字号）：给大字号留出下降部空间
_MIN_BODY_LINE = 1.15        # 正文精确行距下限（相对字号）：再紧就会压到字
_WRAP_DENSITY = 0.94         # 断行估算折算系数（与旧「每行 46 字」口径一致）
_BULLET_INDENT_PT = 13.6     # 项目符号的悬挂缩进 0.48cm

# 一页收不进去时的收紧阶梯：(字号系数, 段距系数, 行距系数)
# 顺序即观感代价：先压段距，再微调行距，实在不行才缩字号。
_FIT_LADDER: Tuple[Tuple[float, float, float], ...] = (
    (1.00, 0.60, 1.00),
    (1.00, 0.35, 1.00),
    (1.00, 0.20, 0.96),
    (0.97, 0.20, 0.94),
    (0.94, 0.15, 0.92),
)
_FIT_TOLERANCE = 1.35        # 超过一页 35% 以上视为「内容确实是两页」，不再硬收
_FIT_SAFETY = 0.97           # 估算留 3% 余量：Word 实际断行与估算总有小差

# 内容偏少时的铺满策略：按「行距 → 模块间距」两级递进，字号不动
_TARGET_FILL = 0.93          # 目标填充率
_MAX_LEAD = 1.12             # 行距最多放宽 12%（均匀放开，观感最自然）
_MAX_GAP_MULT = 1.8          # 模块间距最多放大到 1.8 倍（再大就不像简历、像海报了）

COLOR_TEXT = RGBColor(0x1F, 0x1F, 0x1F)
COLOR_MUTED = RGBColor(0x44, 0x44, 0x44)

_PPR_ORDER = (
    "w:shd", "w:tabs", "w:suppressAutoHyphens", "w:kinsoku", "w:wordWrap",
    "w:overflowPunct", "w:topLinePunct", "w:autoSpaceDE", "w:autoSpaceDN",
    "w:bidi", "w:adjustRightInd", "w:snapToGrid", "w:spacing", "w:ind",
    "w:contextualSpacing", "w:mirrorIndents", "w:suppressOverlap", "w:jc",
    "w:textDirection", "w:textAlignment", "w:textboxTightWrap", "w:outlineLvl",
    "w:divId", "w:cnfStyle", "w:rPr", "w:sectPr", "w:pPrChange",
)


def _tint(hex_color: str, white_ratio: float) -> str:
    """把颜色往白色调淡，返回 hex 字符串（标题色块底纹用）。"""
    value = hex_color.lstrip("#")
    parts = []
    for i in (0, 2, 4):
        channel = int(value[i:i + 2], 16)
        parts.append(round(channel * (1 - white_ratio) + 255 * white_ratio))
    return "%02X%02X%02X" % tuple(parts)


class _Style:
    """把 ResumeTemplate 适配成 python-docx 需要的单位与属性名。"""

    def __init__(self, tpl: templates.ResumeTemplate):
        self.tpl = tpl
        self.margin = Cm(tpl.margin_cm)
        self.body = Pt(tpl.body_pt)
        self.name = Pt(tpl.name_pt)
        self.heading = Pt(tpl.heading_pt)
        self.contact = Pt(tpl.contact_pt)
        self.line = tpl.line
        # 正文精确行距（pt）：与 PDF 引擎同口径，避免 Word 把「1.18 倍」当成 1.18×字体自然行高
        self.body_line_pt = _body_line_pt(tpl)
        self.heading_line_pt = tpl.heading_pt * _HEAD_LINE_FACTOR
        self.bullet_after = tpl.bullet_after
        self.block_after = tpl.block_after
        self.heading_before = tpl.heading_before
        self.heading_after = tpl.heading_after
        self.heading_style = tpl.heading_style
        self.name_align = tpl.name_align
        self.bullet_char = tpl.bullet_char
        self.accent_hex = tpl.accent_hex
        self.accent = RGBColor.from_string(tpl.accent_hex)
        # 文字专用色：装饰（色块/横线/底纹）沿用主色，文字压深到达标对比度
        self.text_accent_hex = tpl.text_accent_hex
        self.text_accent = RGBColor.from_string(tpl.text_accent_hex)
        self.font_cn = tpl.font_cn
        self.font_en = tpl.font_en
        self.compact = tpl.compact

    @property
    def content_width(self) -> Any:
        return Cm(PAGE_W_CM - 2 * self.tpl.margin_cm)


# ============ 底层排版原语 ============


def _apply_font(run, size, bold: bool = False, color: Optional[RGBColor] = None,
                style: Optional[_Style] = None) -> None:
    """中文走模板指定的中文字体、西文走对应西文字体（成熟中文简历的通行做法）。"""
    font_en = style.font_en if style else "Arial"
    font_cn = style.font_cn if style else "微软雅黑"
    run.font.size = size
    run.font.bold = bold
    if color is not None:
        run.font.color.rgb = color
    run.font.name = font_en
    rPr = run._element.get_or_add_rPr()
    rFonts = rPr.find(qn("w:rFonts"))
    if rFonts is None:
        rFonts = OxmlElement("w:rFonts")
        rPr.append(rFonts)
    rFonts.set(qn("w:ascii"), font_en)
    rFonts.set(qn("w:hAnsi"), font_en)
    rFonts.set(qn("w:eastAsia"), font_cn)


def _para(doc, *, space_before: float = 0, space_after: float = 0, line: Optional[float] = None,
          exact_line_pt: Optional[float] = None,
          align=None, keep_next: bool = False, keep_together: bool = True,
          left_indent=None, first_line_indent=None):
    """建段落。

    line：倍数行距（Word 按字体自然行高算，只用于页头/标题这类对留白不敏感的行）。
    exact_line_pt：精确行距（pt），正文一律走这个——与 PDF 引擎口径一致，
                   否则 Word 每行会比 PDF 高 30% 以上，导致多出一页。
    """
    p = doc.add_paragraph()
    pf = p.paragraph_format
    if space_before:
        pf.space_before = Pt(space_before)
    if space_after:
        pf.space_after = Pt(space_after)
    if exact_line_pt:
        pf.line_spacing = Pt(exact_line_pt)        # Length → w:lineRule="exact"
    elif line:
        pf.line_spacing = line
    if align is not None:
        pf.alignment = align
    pf.keep_with_next = keep_next
    pf.keep_together = keep_together
    if left_indent is not None:
        pf.left_indent = left_indent
    if first_line_indent is not None:
        pf.first_line_indent = first_line_indent
    return p


def _bottom_border(paragraph, color_hex: str = "4F46E5", size: int = 6, space: int = 2) -> None:
    """给段落加下边框（小节标题分隔线）。"""
    pPr = paragraph._p.get_or_add_pPr()
    pBdr = OxmlElement("w:pBdr")
    bottom = OxmlElement("w:bottom")
    bottom.set(qn("w:val"), "single")
    bottom.set(qn("w:sz"), str(size))
    bottom.set(qn("w:space"), str(space))
    bottom.set(qn("w:color"), color_hex)
    pBdr.append(bottom)
    try:
        pPr.insert_element_before(pBdr, *_PPR_ORDER)
    except Exception:
        pPr.append(pBdr)


def _shade(paragraph, fill_hex: str) -> None:
    """给段落加底纹（标题色块）。"""
    pPr = paragraph._p.get_or_add_pPr()
    shd = OxmlElement("w:shd")
    shd.set(qn("w:val"), "clear")
    shd.set(qn("w:color"), "auto")
    shd.set(qn("w:fill"), fill_hex)
    try:
        pPr.insert_element_before(shd, *_PPR_ORDER)
    except Exception:
        pPr.append(shd)


def _center_line(doc, text: str, style: _Style, size, bold: bool = False,
                 color: Optional[RGBColor] = None, space_after: float = 1,
                 border: bool = False, align: Optional[str] = None):
    target = align or style.name_align
    alignment = (WD_ALIGN_PARAGRAPH.LEFT if target == "left"
                 else WD_ALIGN_PARAGRAPH.CENTER)
    line_pt = float(size.pt) * _HEAD_LINE_FACTOR
    p = _para(doc, space_after=space_after, exact_line_pt=line_pt,
              align=alignment, keep_together=True)
    run = p.add_run(text)
    _apply_font(run, size, bold=bold, color=color, style=style)
    if border:
        _bottom_border(p, style.accent_hex)
    return p


def _section_heading(doc, text: str, style: _Style):
    """按模板的 heading_style 渲染小节标题：underline / plain / block / center。"""
    hs = style.heading_style

    if hs == "block":
        p = _para(doc, space_before=style.heading_before, space_after=style.heading_after,
                  exact_line_pt=style.heading_line_pt, keep_next=True, keep_together=True)
        _shade(p, _tint(style.accent_hex, 0.86))
        run = p.add_run(" " + text + " ")
        _apply_font(run, style.heading, bold=True, color=style.text_accent, style=style)
        return p

    if hs == "center":
        p = _para(doc, space_before=style.heading_before, space_after=style.heading_after,
                  exact_line_pt=style.heading_line_pt, align=WD_ALIGN_PARAGRAPH.CENTER,
                  keep_next=True, keep_together=True)
        run = p.add_run(text)
        _apply_font(run, style.heading, bold=True, color=style.text_accent, style=style)
        _bottom_border(p, style.accent_hex)
        return p

    p = _para(doc, space_before=style.heading_before, space_after=style.heading_after,
              exact_line_pt=style.heading_line_pt, keep_next=True, keep_together=True)
    run = p.add_run(text)
    _apply_font(run, style.heading, bold=True, color=style.text_accent, style=style)
    if hs == "underline":
        _bottom_border(p, style.accent_hex)
    return p


def _two_col(doc, left_text: str, right_text: str, style: _Style,
             space_before: float = 0, space_after: float = 2,
             bold_left: bool = True, keep_next: bool = False):
    """左「名称（角色）」+ 右「时间」——用右对齐制表位实现，不是塞在一行文本里。"""
    p = _para(doc, space_before=space_before, space_after=space_after,
              exact_line_pt=style.body_line_pt,
              keep_next=keep_next, keep_together=True)
    p.paragraph_format.tab_stops.add_tab_stop(style.content_width, WD_TAB_ALIGNMENT.RIGHT)
    if left_text:
        run = p.add_run(left_text)
        _apply_font(run, style.body, bold=bold_left, color=COLOR_TEXT, style=style)
    if right_text:
        tab = p.add_run("\t")
        _apply_font(tab, style.body, style=style)
        run = p.add_run(right_text)
        _apply_font(run, style.body, bold=False, color=COLOR_MUTED, style=style)
    return p


def _bullet(doc, text: str, style: _Style):
    p = _para(doc, space_after=style.bullet_after, exact_line_pt=style.body_line_pt,
              keep_together=True, left_indent=Cm(0.48), first_line_indent=Cm(-0.48))
    run = p.add_run(style.bullet_char + " " + text)
    _apply_font(run, style.body, color=COLOR_TEXT, style=style)
    return p


def _plain(doc, text: str, style: _Style, color: Optional[RGBColor] = None, indent: float = 0.0):
    p = _para(doc, space_after=style.bullet_after, exact_line_pt=style.body_line_pt,
              keep_together=True, left_indent=Cm(indent) if indent else None)
    run = p.add_run(text)
    _apply_font(run, style.body, color=color or COLOR_TEXT, style=style)
    return p


# ============ 一页预算 ============


def _content_width_pt(tpl: "templates.ResumeTemplate") -> float:
    return (PAGE_W_CM - 2 * tpl.margin_cm) / 2.54 * 72.0


def _page_height_pt(tpl: "templates.ResumeTemplate") -> float:
    return (PAGE_H_CM - 2 * tpl.margin_cm) / 2.54 * 72.0


def _body_line_pt(tpl: "templates.ResumeTemplate", size_scale: float = 1.0,
                  lead_scale: float = 1.0) -> float:
    """正文每行高度（pt）——与 PDF 引擎同口径：字号 × 行距倍数。

    这里刻意不再乘「字体自然行高系数」：Word 的 N 倍行距是按字体自然行高算的
    （微软雅黑约 1.72em），而 PDF 引擎按字号算，同一个 1.5 倍行距能差出 11pt/行。
    解决办法是导出 Word 时直接写「精确行距」= 字号 × 倍数，让两边口径一致。
    """
    return tpl.body_pt * size_scale * max(tpl.line * lead_scale, _MIN_BODY_LINE)


def _wrap_lines(text: str, size: float, width_pt: float) -> float:
    """估算断行后占几行：中文按 1 个字宽，西文按 0.55 个字宽。"""
    if not str(text or "").strip():
        return 0.0
    units = 0.0
    for ch in str(text):
        units += 1.0 if ord(ch) > 0x2E7F else 0.55
    per_line = max(1.0, width_pt * _WRAP_DENSITY / max(size, 1e-6))
    return max(1.0, math.ceil(units / per_line - 1e-6))


def _estimate_height(blocks: Dict[str, Any],
                     tpl: "templates.ResumeTemplate",
                     size_scale: float = 1.0, gap_scale: float = 1.0,
                     lead_scale: float = 1.0,
                     order: Optional[Any] = None) -> float:
    """估算文档实际占用的垂直高度（pt）。

    逐项镜像 `_render_blocks` 的渲染顺序与段距，所以换模板、加模块会自动跟着变；
    三个缩放系数与 `_adjusted` 的语义一一对应，同时服务于「收进一页」和「铺满一页」。

    顺序由 ``order`` 决定（默认 ``core.sections.BODY_SECTIONS``），**与
    ``_render_blocks`` 取同一个来源**。这一点是硬要求：估高与实际渲染只要
    顺序不一致，就会出现「估出来一页、印出来两页」——而且因为总高度对顺序
    并不敏感（只差段间那几个 pt），这种分歧还极难在测试里被发现。
    """
    body = _body_line_pt(tpl, size_scale, lead_scale)
    head = _HEAD_LINE_FACTOR
    width = _content_width_pt(tpl)
    body_size = tpl.body_pt * size_scale
    heading_size = tpl.heading_pt * size_scale

    height = 0.0

    def lines_of(content: str, size: float, indent_pt: float = 0.0) -> float:
        return _wrap_lines(content, size, max(width - indent_pt, 72.0))

    def add_section() -> None:
        nonlocal height
        height += heading_size * head
        height += (tpl.heading_before + tpl.heading_after) * gap_scale

    def add_plain(content: str, size: float, indent_pt: float = 0.0) -> None:
        nonlocal height
        height += lines_of(content, size, indent_pt) * body
        # 项目间距只在「压缩」时跟着收紧；铺满时保持原设计密度，否则列表会被拉散
        height += tpl.bullet_after * min(gap_scale, 1.0)

    # ---- 页头：姓名 / 求职意向 / 联系方式（固定在最前，不参与排序）----
    if blocks.get("name"):
        height += tpl.name_pt * head + 1.0
    if blocks.get("position"):
        height += body_size * head + 1.0
    if blocks.get("contact_line"):
        height += tpl.contact_pt * head + 5.0       # space_after 2 + 下边框留白 3

    def _summary() -> None:
        nonlocal height
        if not blocks.get("summary"):
            return
        add_section()
        add_plain(blocks["summary"], body_size)

    def _education() -> None:
        nonlocal height
        education = blocks.get("education") or []
        if not education:
            return
        add_section()
        for edu in education:
            height += body + 2.0                    # 两栏标题行（左名称 + 右时间）
            for detail in edu.get("details") or []:
                add_plain(detail, body_size)

    def _experiences() -> None:
        nonlocal height
        experiences = blocks.get("experiences") or []
        if not experiences:
            return
        groups: Dict[str, List[Dict[str, Any]]] = {}
        for exp in experiences:
            groups.setdefault(exp.get("group") or "经历", []).append(exp)
        for items in groups.values():
            add_section()
            for exp in items:
                height += body + 3.0                # 两栏标题行 + space_before 1
                for bullet in exp.get("bullets") or []:
                    add_plain(bullet, body_size, _BULLET_INDENT_PT)

    def _skills() -> None:
        nonlocal height
        if not blocks.get("skills"):
            return
        add_section()
        grouped = blocks.get("skill_groups") or {}
        if grouped:
            for group_name, items in grouped.items():
                if items:
                    add_plain(f"{group_name}：{'、'.join(items)}", body_size)
        else:
            add_plain("、".join(blocks["skills"]), body_size)

    def _certifications() -> None:
        nonlocal height
        if not blocks.get("certifications"):
            return
        add_section()
        add_plain("、".join(blocks["certifications"]), body_size)

    def _honors() -> None:
        nonlocal height
        if not blocks.get("honors"):
            return
        add_section()
        for item in blocks["honors"]:
            add_plain(item, body_size)

    def _extra() -> None:
        nonlocal height
        for extra in blocks.get("extra") or []:
            if not extra.get("lines"):
                continue
            add_section()
            for item in extra["lines"]:
                add_plain(item, body_size)

    estimators = {
        "summary": _summary,
        "education": _education,
        "experiences": _experiences,
        "skills": _skills,
        "certifications": _certifications,
        "honors": _honors,
        "extra": _extra,
    }
    for section_name in normalize_section_order(order):
        action = estimators.get(section_name)
        if action is not None:
            action()

    return height


def _fits_one_page(blocks: Dict[str, Any], tpl: "templates.ResumeTemplate",
                   size_scale: float = 1.0, gap_scale: float = 1.0,
                   lead_scale: float = 1.0,
                   order: Optional[Any] = None) -> bool:
    height = _estimate_height(blocks, tpl, size_scale, gap_scale, lead_scale, order)
    return height <= _page_height_pt(tpl) * _FIT_SAFETY


def _adjusted(tpl: "templates.ResumeTemplate", size_scale: float,
              gap_scale: float, lead_scale: float) -> "templates.ResumeTemplate":
    """按系数调整模板（返回副本，不动全局模板单例）。

    字号只动正文与标题，姓名/联系方式保持原大小——页头缩水比多占两行更难看。
    """
    if size_scale == 1.0 and gap_scale == 1.0 and lead_scale == 1.0:
        return tpl
    out = copy.copy(tpl)
    for attr in ("body_pt", "heading_pt"):
        setattr(out, attr, getattr(tpl, attr) * size_scale)
    for attr in ("heading_before", "heading_after", "block_after"):
        setattr(out, attr, max(0.0, getattr(tpl, attr) * gap_scale))
    # 项目间距不参与「铺满」，只参与「压缩」——放大它只会让要点列表变散
    out.bullet_after = max(0.0, tpl.bullet_after * min(gap_scale, 1.0))
    out.line = tpl.line * lead_scale
    return out


def _fit_one_page(blocks: Dict[str, Any],
                  tpl: "templates.ResumeTemplate",
                  order: Optional[Any] = None) -> "templates.ResumeTemplate":
    """把内容收进一页：逐档压段距/行距/字号，取第一个装得下的档位。

    只在「刚好溢出一点」时收紧；超出 35% 以上说明内容确实是两页，
    硬缩字号会把正文压到 9pt 以下，比多一页更糟。本来装得下就原样返回。
    """
    height = _estimate_height(blocks, tpl, order=order)
    available = _page_height_pt(tpl)
    if height <= available * _FIT_SAFETY:
        return tpl
    if height > available * _FIT_TOLERANCE:
        return tpl                                # 内容确实是两页，按原版式输出

    for size_scale, gap_scale, lead_scale in _FIT_LADDER:
        if size_scale * tpl.body_pt < 9.0:        # 正文字号下限，不再往下压
            break
        if _fits_one_page(blocks, tpl, size_scale, gap_scale, lead_scale, order):
            return _adjusted(tpl, size_scale, gap_scale, lead_scale)

    # 所有档位都装不下：用最紧的一档兜底（内容量已在容差内，通常能收住）
    size_scale, gap_scale, lead_scale = _FIT_LADDER[-1]
    return _adjusted(tpl, max(size_scale, 9.0 / tpl.body_pt), gap_scale, lead_scale)


def _fill_one_page(blocks: Dict[str, Any],
                   tpl: "templates.ResumeTemplate",
                   order: Optional[Any] = None) -> "templates.ResumeTemplate":
    """内容偏少时铺满一页：先放行距，再撑模块间距，字号不动。

    与 PDF 引擎同一套策略，目的是让同一份简历导出 PDF 和 Word 长得一样：
    PDF 会自己铺满到目标填充率，Word 若照原样输出就会在底部空出一大块。
    """
    available = _page_height_pt(tpl)
    target = available * _TARGET_FILL
    if _estimate_height(blocks, tpl, order=order) >= target:
        return tpl

    # 第一步：均匀放宽行距（观感最自然）
    lead_lo, lead_hi = 1.0, _MAX_LEAD
    for _ in range(6):
        mid = (lead_lo + lead_hi) / 2.0
        probe = _estimate_height(blocks, tpl, 1.0, 1.0, mid, order)
        if probe > target or not _fits_one_page(blocks, tpl, 1.0, 1.0, mid, order):
            lead_hi = mid
        else:
            lead_lo = mid
    lead = lead_lo

    # 第二步：还不够满就撑模块间距（最多放大到 _MAX_GAP_MULT 倍）
    gap_lo, gap_hi = 1.0, _MAX_GAP_MULT
    for _ in range(6):
        mid = (gap_lo + gap_hi) / 2.0
        probe = _estimate_height(blocks, tpl, 1.0, mid, lead, order)
        if probe > target or not _fits_one_page(blocks, tpl, 1.0, mid, lead, order):
            gap_hi = mid
        else:
            gap_lo = mid
    return _adjusted(tpl, 1.0, gap_lo, lead)


def _resolve_template(blocks: Dict[str, Any],
                      template_key: Optional[str],
                      order: Optional[Any] = None,
                      appearance: Optional[templates.ResumeAppearance] = None
                      ) -> "templates.ResumeTemplate":
    """指定了模板就尊重用户选择；没指定时按内容量在经典/紧凑之间自动选。

    无论哪条分支，最后都会「先收进一页、再铺满一页」：PDF 侧同样是
    「一页硬约束 + 内容偏少时铺满」，两边口径必须一致，
    否则同一份简历导出 PDF 和 Word 会一个 1 页、一个 2 页，观感也对不上。

    外观在**一页调优之前**叠加：用户把字号放大到 1.2 之后，收进一页的
    阶梯必须按放大后的字号去算，否则会出现「估高时按 10.5pt、实际印 12.6pt」的错位。
    """
    if template_key:
        chosen = templates.get_template(template_key)
    else:
        default = templates.get_template(templates.DEFAULT_TEMPLATE_KEY)
        chosen = (templates.get_template("compact")
                  if not _fits_one_page(blocks, default, order=order) else default)
    chosen = templates.apply_appearance(chosen, appearance)
    return _fill_one_page(blocks, _fit_one_page(blocks, chosen, order), order)


def estimate_layout(text: str = "", structured: Optional[Dict[str, Any]] = None,
                    order: Optional[Any] = None,
                    appearance: Optional[templates.ResumeAppearance] = None
                    ) -> Dict[str, Any]:
    """预估页数与是否进入紧凑版式，供 UI 提示。"""
    blocks = from_structured(structured) if structured else parse_resume_text(text)
    default = templates.apply_appearance(
        templates.get_template(templates.DEFAULT_TEMPLATE_KEY), appearance)
    height = _estimate_height(blocks, default, order=order)
    available = _page_height_pt(default)
    line_pt = _body_line_pt(default)
    return {
        "lines": round(height / line_pt, 1),
        "budget": round(available / line_pt, 1),
        "pages": 1 if height <= available * _FIT_SAFETY
                 else max(2, math.ceil(height / available)),
        "compact": not _fits_one_page(blocks, default, order=order),
    }


def _setup_document(style: _Style):
    doc = Document()
    section = doc.sections[0]
    section.page_width = Cm(PAGE_W_CM)
    section.page_height = Cm(PAGE_H_CM)
    section.top_margin = section.bottom_margin = style.margin
    section.left_margin = section.right_margin = style.margin

    normal = doc.styles["Normal"]
    normal.font.name = style.font_en
    normal.font.size = style.body
    normal.element.rPr.rFonts.set(qn("w:eastAsia"), style.font_cn)
    return doc


def _render_blocks(doc, blocks: Dict[str, Any], style: _Style,
                   name_fallback: str = "", order: Optional[Any] = None) -> None:
    name = blocks.get("name") or name_fallback
    if name:
        _center_line(doc, name, style, style.name, bold=True, color=COLOR_TEXT,
                     space_after=1, align=style.name_align)

    if blocks.get("position"):
        _center_line(doc, blocks["position"], style, style.body, bold=True,
                     color=style.text_accent, space_after=1, align=style.name_align)

    if blocks.get("contact_line"):
        contact = re.sub(r"\s*[|｜]\s*", " · ", blocks["contact_line"])
        _center_line(doc, contact, style, style.contact, color=COLOR_MUTED,
                     space_after=2, border=True, align=style.name_align)

    # ---- 正文分区（顺序与 _estimate_height 同源，见 core/sections.py）----
    def _summary() -> None:
        if not blocks.get("summary"):
            return
        _section_heading(doc, SECTION_LABELS["summary"], style)
        _plain(doc, blocks["summary"], style)

    def _education() -> None:
        education = blocks.get("education") or []
        if not education:
            return
        _section_heading(doc, SECTION_LABELS["education"], style)
        for edu in education:
            details = edu.get("details") or []
            _two_col(doc, edu.get("left", ""), edu.get("period", ""), style,
                     keep_next=bool(details))
            for detail in details:
                _plain(doc, detail, style, color=COLOR_MUTED)

    def _experiences() -> None:
        experiences = blocks.get("experiences") or []
        if not experiences:
            return
        # 按类型分组（项目经历 / 实习经历 / 工作经历），保持首次出现顺序
        groups: Dict[str, List[Dict[str, Any]]] = {}
        for exp in experiences:
            groups.setdefault(exp.get("group") or "经历", []).append(exp)
        for group_name, items in groups.items():
            _section_heading(doc, group_name, style)
            for exp in items:
                bullets = exp.get("bullets") or []
                _two_col(doc, exp.get("left", ""), exp.get("period", ""), style,
                         space_before=1, keep_next=bool(bullets))
                for bullet in bullets:
                    _bullet(doc, bullet, style)

    def _skills() -> None:
        if not blocks.get("skills"):
            return
        _section_heading(doc, SECTION_LABELS["skills"], style)
        groups = blocks.get("skill_groups") or {}
        if groups:
            for group_name, items in groups.items():
                if items:
                    _plain(doc, f"{group_name}：{'、'.join(items)}", style)
        else:
            _plain(doc, "、".join(blocks["skills"]), style)

    def _certifications() -> None:
        if not blocks.get("certifications"):
            return
        _section_heading(doc, SECTION_LABELS["certifications"], style)
        _plain(doc, "、".join(blocks["certifications"]), style)

    def _honors() -> None:
        if not blocks.get("honors"):
            return
        _section_heading(doc, SECTION_LABELS["honors"], style)
        for item in blocks["honors"]:
            _plain(doc, item, style)

    def _extra() -> None:
        for extra in blocks.get("extra") or []:
            if not extra.get("lines"):
                continue
            _section_heading(doc, extra.get("title") or SECTION_LABELS["extra"], style)
            for item in extra["lines"]:
                _plain(doc, item, style)

    renderers = {
        "summary": _summary,
        "education": _education,
        "experiences": _experiences,
        "skills": _skills,
        "certifications": _certifications,
        "honors": _honors,
        "extra": _extra,
    }
    for section_name in normalize_section_order(order):
        action = renderers.get(section_name)
        if action is not None:
            action()


def _render_plain(doc, text: str, style: _Style, name_fallback: str = "") -> None:
    """兜底：文本结构无法识别时，至少保证版式与字号是专业规格。"""
    lines = [line.strip() for line in (text or "").splitlines() if line.strip()]
    if not lines:
        return
    head = name_fallback or lines[0]
    _center_line(doc, head, style, style.name, bold=True, color=COLOR_TEXT, space_after=2,
                 border=True, align=style.name_align)
    for line in lines[1:]:
        # 前缀要认得全：既认用户常写的 "- / • / ·"，也认我们自己的模板符号 ●，
        # 否则把生成结果再导入一次会把项目符号当成正文开头。
        if line.startswith(("- ", "• ", "· ", "● ", "○ ", "▪ ", "‣ ", "– ", "— ")):
            _bullet(doc, line.lstrip("-•·●○▪‣–— ").strip(), style)
        else:
            _plain(doc, line, style)


# ============ 对外接口 ============


def export_txt(resume_text: str, filepath: str) -> str:
    with open(filepath, "w", encoding="utf-8") as f:
        f.write(resume_text or "")
    return filepath


def export_docx_from_blocks(blocks: Dict[str, Any], filepath: str,
                            template_key: Optional[str] = None,
                            order: Optional[Any] = None,
                            appearance: Optional[templates.ResumeAppearance] = None
                            ) -> str:
    """从已解析好的 blocks 导出（优化页只有纯文本，走这条路径）。"""
    style = _Style(_resolve_template(blocks, template_key, order, appearance))
    doc = _setup_document(style)
    _render_blocks(doc, blocks, style,
                   name_fallback=str(blocks.get("name") or ""), order=order)
    doc.save(filepath)
    return filepath


def export_docx_from_structured(structured: Dict[str, Any], filepath: str,
                                template_key: Optional[str] = None,
                                order: Optional[Any] = None,
                                appearance: Optional[templates.ResumeAppearance] = None
                                ) -> str:
    return export_docx_from_blocks(from_structured(structured), filepath,
                                   template_key, order, appearance)


def export_docx(resume_text: str, filepath: str, name: str = "",
                structured: Optional[Dict[str, Any]] = None,
                template_key: Optional[str] = None,
                order: Optional[Any] = None,
                appearance: Optional[templates.ResumeAppearance] = None) -> str:
    """导出 Word。有结构化数据时直出（信息无损），否则解析纯文本。

    兼容旧调用：export_docx(text, path, name)。
    """
    if isinstance(structured, dict) and structured:
        return export_docx_from_structured(structured, filepath, template_key,
                                           order, appearance)

    blocks = parse_resume_text(resume_text)
    # 旧代码这里写的是 `not blocks.get("section_order") and ...`，但
    # parse_resume_text 从来不产出 section_order 这个键，所以那一段判断
    # 恒为真 —— 是个「看起来在判分节顺序、实际只在判有没有正文」的幌子。
    # 这里改成直接判「有没有可识别的正文分区」，语义与旧行为逐字等价
    # （三个条件都为空才走纯文本兜底），只是不再假装在读一个不存在的字段。
    if not blocks.get("experiences") and not blocks.get("education") \
            and not blocks.get("skills"):
        style = _Style(templates.resolve_template(
            template_key or templates.DEFAULT_TEMPLATE_KEY, appearance))
        doc = _setup_document(style)
        _render_plain(doc, resume_text, style, name_fallback=name)
        doc.save(filepath)
        return filepath

    style = _Style(_resolve_template(blocks, template_key, order, appearance))
    doc = _setup_document(style)
    _render_blocks(doc, blocks, style, name_fallback=name, order=order)
    doc.save(filepath)
    return filepath


def default_export_path(directory: str, stem: str, ext: str) -> str:
    os.makedirs(directory, exist_ok=True)
    safe = re.sub(r'[\\/:*?"<>|]', "_", stem or "简历").strip() or "简历"
    return os.path.join(directory, f"{safe}.{ext}")
