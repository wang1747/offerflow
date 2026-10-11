"""纯标准库的简历 PDF 导出。

为什么不用 reportlab / docx2pdf：
- 本机既没有 Word 也没有 LibreOffice，docx2pdf、soffice 全部不可用；
- 打包成 EXE 时要尽量少加依赖，纯标准库能让产物体积几乎不变。

做法：解析系统中文字体 → 子集化（18MB 压到 20KB）→ 手写 PDF 对象
（Type0 + CIDFontType2 + FontFile2 + ToUnicode），文本用 Identity-H 编码。
"""

from __future__ import annotations

import copy
import os
import re
import zlib
from typing import Any, Dict, List, Optional, Tuple

from ..core.font_subset import TrueTypeFont, find_cjk_font
from ..core.resume_review import from_structured, parse_resume_text
from ..core.sections import SECTION_LABELS, normalize_section_order
from . import templates

# A4
PAGE_W_PT = 595.276
PAGE_H_PT = 841.890
CM_TO_PT = 28.3465


# ============ 绘制指令引擎 ============


class _TextEngine:
    """负责文本测量、断行，并记录绘制调用。

    不能先收集"可能要用的字符"再做子集化——代码拼接出来的字符（如技能行里的
    全角冒号）很容易漏掉，漏掉就会在 PDF 里变成问号。所以用两遍法：
    先布局并把所有绘制调用记录下来，取真实用到的字符做子集，再编码成 PDF 指令。
    """

    def __init__(self, font: TrueTypeFont):
        self.font = font
        self.calls: List[tuple] = []
        self._width_cache: Dict[Tuple[str, float], float] = {}

    # ---- 度量 ----

    def width(self, text: str, size: float) -> float:
        key = (text, size)
        cached = self._width_cache.get(key)
        if cached is not None:
            return cached
        units = 0
        for ch in text:
            gid = self.font.glyph_id(ord(ch))
            units += self.font.advance_width(gid) if gid else self.font.advance_width(0)
        value = units * size / self.font.units_per_em
        self._width_cache[key] = value
        return value

    @staticmethod
    def _tokenize(text: str) -> List[str]:
        """切成「不该跨行拆开的最小单元」：中文逐字、西文词整体、其余单字符。

        为什么不能像以前那样「逐字符累加、超宽就断」：那样在中文行里遇到英文
        技术词（``SQLAlchemy``）会从中间硬切开，排出 ``SQLAlc / hemy``。
        简历里技术名词密集，这种断法一眼就是排版事故。按词元累加后，
        英文词要么整体留在本行、要么整体挪到下一行。
        """
        out: List[str] = []
        buf = ""
        for ch in text:
            if ord(ch) < 128 and (ch.isalnum() or ch in "._-+/#@'’"):
                buf += ch                      # 英文/数字连写成一个词
            else:
                if buf:
                    out.append(buf)
                    buf = ""
                out.append(ch)                 # 中文/标点/空格各自成一个单元
        if buf:
            out.append(buf)
        return out

    def wrap(self, text: str, size: float, max_width: float) -> List[str]:
        """按可用宽度断行：中文可逐字断，西文词整体换行（不在词中间切开）。"""
        text = re.sub(r"\s+", " ", (text or "").replace("\r", "")).strip()
        if not text:
            return []
        lines: List[str] = []
        current = ""
        for tok in self._tokenize(text):
            trial = current + tok
            if current and self.width(trial, size) > max_width:
                lines.append(current.rstrip())
                current = "" if tok == " " else tok
            else:
                current = trial
        if current.strip():
            lines.append(current.rstrip())
        return lines

    # ---- 绘制 ----

    def text(self, content: str, x: float, y: float, size: float,
             rgb: Tuple[float, float, float], bold: bool = False,
             align: str = "left", max_width: Optional[float] = None) -> None:
        if not content:
            return
        # 对齐在记录阶段就算成最终坐标（用原字体度量，与子集无关）
        if align == "center" and max_width:
            x = x + (max_width - self.width(content, size)) / 2.0
        elif align == "right" and max_width:
            x = x + max_width - self.width(content, size)
        self.calls.append(("t", content, x, y, size, rgb, bold))

    def line(self, x1: float, y1: float, x2: float, y2: float,
             rgb: Tuple[float, float, float], width: float = 0.6) -> None:
        self.calls.append(("l", x1, y1, x2, y2, rgb, width))

    def rect(self, x: float, y: float, w: float, h: float,
             rgb: Tuple[float, float, float]) -> None:
        self.calls.append(("r", x, y, w, h, rgb))

    def used_text(self) -> str:
        """本次布局真正会绘制的所有字符。"""
        return "".join(call[1] for call in self.calls if call[0] == "t")

    def build_content(self, subset_map: Dict[int, int]) -> bytes:
        """把记录的调用编码成 PDF 内容流（字体用 Identity-H，输出字形编号）。"""
        ops: List[str] = []
        fallback = subset_map.get(ord("?"), 0)
        for call in self.calls:
            if call[0] == "t":
                _tag, content, x, y, size, rgb, bold = call
                codes = []
                for ch in content:
                    gid = subset_map.get(ord(ch))
                    codes.append(f"{(gid if gid is not None else fallback):04X}")
                r, g, b = rgb
                parts = [f"BT /F1 {size:.2f} Tf {r:.3f} {g:.3f} {b:.3f} rg"]
                if bold:
                    parts.append(f"{size * 0.035:.2f} w 2 Tr")
                parts.append(f"1 0 0 1 {x:.2f} {y:.2f} Tm")
                parts.append(f"<{''.join(codes)}> Tj")
                if bold:
                    parts.append("0 Tr")
                parts.append("ET")
                ops.append(" ".join(parts))
            elif call[0] == "l":
                _tag, x1, y1, x2, y2, rgb, width = call
                r, g, b = rgb
                ops.append(f"{r:.3f} {g:.3f} {b:.3f} RG {width:.2f} w "
                           f"{x1:.2f} {y1:.2f} m {x2:.2f} {y2:.2f} l S")
            else:
                _tag, x, y, w, h, rgb = call
                r, g, b = rgb
                ops.append(f"{r:.3f} {g:.3f} {b:.3f} rg "
                           f"{x:.2f} {y:.2f} {w:.2f} {h:.2f} re f")
        return ("\n".join(["q"] + ops + ["Q"])).encode("latin-1")


def _rgb(hex_color: str) -> Tuple[float, float, float]:
    value = hex_color.lstrip("#")
    return tuple(int(value[i:i + 2], 16) / 255.0 for i in (0, 2, 4))  # type: ignore[return-value]


def _mix(hex_color: str, white_ratio: float) -> Tuple[float, float, float]:
    """把颜色往白色方向调淡：0 = 原色，1 = 纯白。用于标题色块的底纹。"""
    base = _rgb(hex_color)
    return tuple(c * (1.0 - white_ratio) + white_ratio for c in base)  # type: ignore[return-value]


COLOR_TEXT = (0.12, 0.12, 0.12)
COLOR_MUTED = (0.28, 0.28, 0.28)


# ============ PDF 对象写入 ============


class _PdfWriter:
    def __init__(self) -> None:
        self.bodies: List[bytes] = []

    def add(self, body: bytes) -> int:
        self.bodies.append(body)
        return len(self.bodies)

    def add_stream(self, payload: bytes, extra: str = "", compress: bool = True) -> int:
        if compress:
            data = zlib.compress(payload, 9)
            head = f"<< /Length {len(data)} /Filter /FlateDecode {extra} >>"
        else:
            data = payload
            head = f"<< /Length {len(data)} {extra} >>"
        return self.add(head.encode("latin-1") + b"\nstream\n" + data + b"\nendstream")

    def build(self, root: int) -> bytes:
        out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
        offsets: List[int] = []
        for number, body in enumerate(self.bodies, start=1):
            offsets.append(len(out))
            out += f"{number} 0 obj\n".encode("latin-1") + body + b"\nendobj\n"
        xref_at = len(out)
        count = len(self.bodies) + 1
        out += f"xref\n0 {count}\n".encode("latin-1")
        out += b"0000000000 65535 f \n"
        for offset in offsets:
            out += f"{offset:010d} 00000 n \n".encode("latin-1")
        out += (f"trailer\n<< /Size {count} /Root {root} 0 R >>\n"
                f"startxref\n{xref_at}\n%%EOF\n").encode("latin-1")
        return bytes(out)


def _to_unicode_cmap(inverse: Dict[int, int]) -> bytes:
    header = (
        "/CIDInit /ProcSet findresource begin\n"
        "12 dict begin\nbegincmap\n"
        "/CIDSystemInfo << /Registry (Adobe) /Ordering (UCS) /Supplement 0 >> def\n"
        "/CMapName /Adobe-Identity-UCS def\n/CMapType 2 def\n"
        "1 begincodespacerange\n<0000> <FFFF>\nendcodespacerange\n"
    )
    items = sorted((gid, code) for gid, code in inverse.items() if gid != 0)
    body = []
    for start in range(0, len(items), 100):
        chunk = items[start:start + 100]
        body.append(f"{len(chunk)} beginbfchar\n")
        for gid, code in chunk:
            body.append(f"<{gid:04X}> <{code:04X}>\n")
        body.append("endbfchar\n")
    footer = "endcmap\nCMapName currentdict /CMap defineresource pop\nend\nend\n"
    return (header + "".join(body) + footer).encode("latin-1")


_FONT_NAME_MAP = {
    "微软雅黑": "MicrosoftYaHei",
    "宋体": "SimSun",
    "黑体": "SimHei",
    "等线": "DengXian",
    "楷体": "KaiTi",
}


def _font_postscript_name(cn_name: str) -> str:
    return _FONT_NAME_MAP.get(cn_name, "CJKFont")


# ============ 版式渲染 ============


class _Cursor:
    def __init__(self, top: float):
        self.y = top

    def down(self, amount: float) -> None:
        self.y -= amount


class _Column:
    """一栏：左/右边界 + 自己的游标 + 自己的「首个区块」计数。

    单栏时只建一个；双栏时建主/副两栏，各自独立地下移游标。
    ``gap_count`` 必须**按栏**分开算 —— 它是 heading() 里「第一个小节紧接页头，
    额外间距要收敛」的判据；若两栏共用一个计数，副栏的第一个标题会被当成
    「不是第一个」而多出一截间距，副栏就会从半空开始。
    """

    __slots__ = ("left", "right", "width", "cursor", "gap_count")

    def __init__(self, left: float, right: float, top: float):
        self.left = left
        self.right = right
        self.width = right - left
        self.cursor = _Cursor(top)
        self.gap_count = 0


def _scaled_template(tpl: templates.ResumeTemplate, scale: float) -> templates.ResumeTemplate:
    """按比例缩放字号与段距，用于「内容超页时自动收到一页」。

    字号只做小幅调整（简历正文字号一旦超过 12pt 就不专业了），
    空格留给段距承担，所以段距的缩放系数比字号大。
    """
    scaled = copy.copy(tpl)
    for attr in ("name_pt", "position_pt", "contact_pt", "heading_pt", "body_pt"):
        setattr(scaled, attr, getattr(tpl, attr) * scale)
    factor = 1.0 + (scale - 1.0) * 1.4
    for attr in ("heading_before", "heading_after", "bullet_after", "block_after"):
        setattr(scaled, attr, getattr(tpl, attr) * factor)
    return scaled


# 内容偏少时的铺满策略：按「行距 → 模块间距 → 顶部补白」三级递进，
# 不再继续放大字号（正文超过 12pt 就不像简历了）
_TARGET_FILL = 0.93          # 目标填充率
_MAX_LEAD = 1.12             # 行距最多放宽 12%（均匀放开，观感最自然）
_MAX_SPREAD = 12.0           # 单个模块间距最多再撑多少 pt
_FIRST_GAP_CAP = 8.0         # 第一个小节与页头之间的额外间距上限（别把页头孤立起来）
_TOP_PAD_CAP = 42.0          # 顶部最多下移多少 pt（≈1.5cm），避免姓名紧贴页顶显得空


def _tuned_template(tpl: templates.ResumeTemplate, spread: float = 0.0,
                    lead: float = 1.0) -> templates.ResumeTemplate:
    """放宽行距、撑开模块间距，字号保持不变。

    为什么不用继续放大字号：一页撑不满时把正文放到 12pt 以上会失去专业感；
    优先动行距是因为行距均匀放开看起来是「排版留白」，而只撑模块间距
    会让小节之间出现比行高还大的空洞，显得散。
    """
    if spread == 0.0 and lead == 1.0:
        return tpl
    out = copy.copy(tpl)
    out.line = tpl.line * lead
    out.heading_before = tpl.heading_before + spread
    out.block_after = tpl.block_after + spread
    return out


def _render(tpl: templates.ResumeTemplate, blocks: Dict[str, Any],
            engine: _TextEngine, page_w: float, page_h: float,
            name_fallback: str = "", scale: float = 1.0,
            spread: float = 0.0, lead: float = 1.0,
            top_pad: float = 0.0, order: Optional[Any] = None) -> Dict[str, Any]:
    if scale != 1.0:
        tpl = _scaled_template(tpl, scale)
    tpl = _tuned_template(tpl, spread, lead)
    margin = tpl.margin_cm * CM_TO_PT
    left = margin
    right = page_w - margin
    content_w = right - left
    accent = _rgb(tpl.accent_hex)
    # 文字专用色：装饰仍用主色，文字压深到达标对比度
    text_accent = _rgb(tpl.text_accent_hex)
    # 页头（姓名/意向/联系方式）始终横跨全宽，先用独立游标量出它的底边，
    # 正文再从那条线开始分栏。
    header_cursor = _Cursor(page_h - margin - top_pad)
    overflow = False
    # 当前活动栏。用单元素列表承载，是为了让下面的绘制闭包能读到
    # 「这一笔该落在哪一栏」，而不必给每个绘制函数都加一个栏参数。
    cur: List["_Column"] = []

    def guard(needed: float) -> None:
        nonlocal overflow
        if cur and cur[0].cursor.y - needed < margin:
            overflow = True

    # ---- 姓名 ----
    name = blocks.get("name") or name_fallback
    if name:
        header_cursor.down(tpl.name_pt)
        engine.text(name, left, header_cursor.y, tpl.name_pt, COLOR_TEXT, bold=True,
                    align=tpl.name_align, max_width=content_w)
        header_cursor.down(tpl.name_pt * 0.35)

    # ---- 求职意向 ----
    if blocks.get("position"):
        header_cursor.down(tpl.position_pt)
        engine.text(blocks["position"], left, header_cursor.y, tpl.position_pt, text_accent,
                    align=tpl.name_align, max_width=content_w)
        header_cursor.down(tpl.position_pt * 0.3)

    # ---- 联系方式 ----
    if blocks.get("contact_line"):
        contact = re.sub(r"\s*[|｜]\s*", "  ·  ", blocks["contact_line"])
        header_cursor.down(tpl.contact_pt + 2)
        engine.text(contact, left, header_cursor.y, tpl.contact_pt, COLOR_MUTED,
                    align=tpl.name_align, max_width=content_w)
        header_cursor.down(tpl.contact_pt * 0.5 + 3)
        header_cursor.down(2)
        engine.line(left, header_cursor.y, right, header_cursor.y, _rgb("D8DAE5"), 0.7)
        header_cursor.down(3)

    # ---- 正文分栏 ----
    # 页头横跨全宽；正文从页头底边开始，只有声明了副栏分区才真的分两栏。
    body_top = header_cursor.y
    if tpl.columns == 2 and tpl.sidebar_sections:
        gutter = 14.0                       # 栏间距（pt）
        main_w = content_w * (1.0 - tpl.sidebar_ratio) - gutter / 2.0
        side_w = content_w * tpl.sidebar_ratio - gutter / 2.0
        main_col = _Column(left, left + main_w, body_top)
        side_col = _Column(right - side_w, right, body_top)
    else:
        main_col = _Column(left, right, body_top)
        side_col = None
    cur.append(main_col)

    def heading(title: str) -> None:
        nonlocal overflow
        col = cur[0]
        gap = tpl.heading_before
        if col.gap_count == 0 and spread:
            # 第一个小节紧接页头，额外间距要收敛，否则姓名会被孤立在页顶
            gap -= max(0.0, spread - _FIRST_GAP_CAP)
        col.gap_count += 1
        col.cursor.down(gap)
        guard(tpl.heading_pt * tpl.line)
        if tpl.heading_style == "block":
            box_h = tpl.heading_pt * 1.7
            col.cursor.down(box_h + 2)
            guard(box_h)
            top = col.cursor.y
            box_w = min(col.width, engine.width(title, tpl.heading_pt) + 20)
            engine.rect(col.left, top, box_w, box_h, _mix(tpl.accent_hex, 0.86))
            baseline = top + (box_h + tpl.heading_pt * 0.72) / 2.0
            engine.text(title, col.left + 9, baseline, tpl.heading_pt, text_accent, bold=True)
            col.cursor.down(tpl.heading_after)
        elif tpl.heading_style == "center":
            col.cursor.down(tpl.heading_pt)
            engine.text(title, col.left, col.cursor.y, tpl.heading_pt, text_accent, bold=True,
                        align="center", max_width=col.width)
            col.cursor.down(3)
            engine.line(col.left, col.cursor.y, col.right, col.cursor.y, accent, 0.8)
            col.cursor.down(tpl.heading_after)
        elif tpl.heading_style == "plain":
            col.cursor.down(tpl.heading_pt)
            engine.text(title, col.left, col.cursor.y, tpl.heading_pt, text_accent, bold=True)
            col.cursor.down(tpl.heading_after)
        else:  # underline
            col.cursor.down(tpl.heading_pt)
            engine.text(title, col.left, col.cursor.y, tpl.heading_pt, text_accent, bold=True)
            col.cursor.down(2)
            engine.line(col.left, col.cursor.y, col.right, col.cursor.y, accent, 0.8)
            col.cursor.down(tpl.heading_after)

    def two_col(left_text: str, right_text: str) -> None:
        col = cur[0]
        col.cursor.down(tpl.body_pt * tpl.line)
        guard(tpl.body_pt)
        engine.text(left_text, col.left, col.cursor.y, tpl.body_pt, COLOR_TEXT, bold=True)
        if right_text:
            engine.text(right_text, col.left, col.cursor.y, tpl.body_pt, COLOR_MUTED,
                        align="right", max_width=col.width)

    def paragraph(text: str, indent: float = 0.0) -> None:
        col = cur[0]
        for line in engine.wrap(text, tpl.body_pt, col.width - indent):
            col.cursor.down(tpl.body_pt * tpl.line)
            guard(tpl.body_pt)
            engine.text(line, col.left + indent, col.cursor.y, tpl.body_pt, COLOR_TEXT)
        col.cursor.down(tpl.bullet_after)

    def bullet(text: str) -> None:
        col = cur[0]
        marker_w = engine.width(tpl.bullet_char + " ", tpl.body_pt)
        lines = engine.wrap(text, tpl.body_pt, col.width - marker_w)
        if not lines:
            return
        col.cursor.down(tpl.body_pt * tpl.line)
        guard(tpl.body_pt)
        engine.text(tpl.bullet_char, col.left, col.cursor.y, tpl.body_pt, accent, bold=True)
        engine.text(lines[0], col.left + marker_w, col.cursor.y, tpl.body_pt, COLOR_TEXT)
        for line in lines[1:]:
            col.cursor.down(tpl.body_pt * tpl.line)
            guard(tpl.body_pt)
            engine.text(line, col.left + marker_w, col.cursor.y, tpl.body_pt, COLOR_TEXT)
        col.cursor.down(tpl.bullet_after)

    # ---- 正文分区（顺序来自 core/sections.py，不再写死）----
    # 原来是七个 if 把顺序钉在函数体里。现在拆成七个互不共享状态的动作，
    # 按 normalize_section_order(order) 迭代调用。
    #
    # 为什么换顺序不影响上面那套一页调优：每个动作只依赖 cursor 的**当前
    # 位置**，整段的 used_height 对顺序不敏感（各段高度之和相同，只有段间
    # 过渡差几个 pt），而 scale/lead/spread/top_pad 四段判据用的正是
    # used_height 这一个标量。heading() 里那个「第一个小节紧接页头」的
    # 特殊处理按 gap_count == 0 判断，也是跟着顺序走的，不需要额外改。
    def _summary() -> None:
        if not blocks.get("summary"):
            return
        heading(SECTION_LABELS["summary"])
        paragraph(blocks["summary"])
        cur[0].cursor.down(tpl.block_after * 0.4)

    def _education() -> None:
        education = blocks.get("education") or []
        if not education:
            return
        heading(SECTION_LABELS["education"])
        for edu in education:
            two_col(edu.get("left", ""), edu.get("period", ""))
            for detail in (edu.get("details") or []):
                paragraph(detail)
        cur[0].cursor.down(tpl.block_after * 0.5)

    def _experiences() -> None:
        experiences = blocks.get("experiences") or []
        if not experiences:
            return
        groups: Dict[str, List[Dict[str, Any]]] = {}
        for exp in experiences:
            groups.setdefault(exp.get("group") or "经历", []).append(exp)
        for group_name, items in groups.items():
            heading(group_name)
            for exp in items:
                two_col(exp.get("left", ""), exp.get("period", ""))
                for item in (exp.get("bullets") or []):
                    bullet(item)
            cur[0].cursor.down(tpl.block_after * 0.5)

    def _skills() -> None:
        if not blocks.get("skills"):
            return
        heading(SECTION_LABELS["skills"])
        grouped = blocks.get("skill_groups") or {}
        if grouped:
            for group_name, items in grouped.items():
                if items:
                    paragraph(f"{group_name}：{'、'.join(items)}")
        else:
            paragraph("、".join(blocks["skills"]))
        cur[0].cursor.down(tpl.block_after * 0.4)

    def _certifications() -> None:
        if not blocks.get("certifications"):
            return
        heading(SECTION_LABELS["certifications"])
        paragraph("、".join(blocks["certifications"]))

    def _honors() -> None:
        if not blocks.get("honors"):
            return
        heading(SECTION_LABELS["honors"])
        for item in blocks["honors"]:
            paragraph(item)

    def _extra() -> None:
        for extra in blocks.get("extra") or []:
            lines = extra.get("lines") or []
            if not lines:
                continue
            heading(extra.get("title") or SECTION_LABELS["extra"])
            for item in lines:
                paragraph(item)

    renderers = {
        "summary": _summary,
        "education": _education,
        "experiences": _experiences,
        "skills": _skills,
        "certifications": _certifications,
        "honors": _honors,
        "extra": _extra,
    }
    sidebar_set = set(tpl.sidebar_sections)
    for section_name in normalize_section_order(order):
        # 切换当前栏：声明为副栏的分区进副栏，其余一律进主栏。
        cur[0] = side_col if (side_col is not None and section_name in sidebar_set) else main_col
        action = renderers.get(section_name)
        if action is not None:
            action()

    # 一页调优的判据：两栏取较高的那一栏（谁先到底谁说了算）。
    # 单栏时 side_col 为 None，退化成原来的「单游标总高度」，行为不变。
    main_used = (page_h - margin) - main_col.cursor.y
    used_height = main_used
    if side_col is not None:
        used_height = max(main_used, (page_h - margin) - side_col.cursor.y)
    return {
        "overflow": overflow,
        "used_height": round(used_height, 1),
        "available_height": round(page_h - 2 * margin, 1),
        "columns": tpl.columns,
        "gap_count": main_col.gap_count,
        "spread": round(spread, 2),
        "lead": round(lead, 3),
        "top_pad": round(top_pad, 1),
    }


def render_pdf_blocks(blocks: Dict[str, Any],
                      template_key: Optional[str] = None,
                      order: Optional[Any] = None,
                      appearance: Optional[templates.ResumeAppearance] = None
                      ) -> Tuple[bytes, Dict[str, Any]]:
    """从已解析好的 blocks 渲染（优化页只有纯文本，走这条路径）。"""
    return _render_pdf(blocks or {}, template_key, order, appearance)


def render_pdf_bytes(structured: Dict[str, Any], name_fallback: str = "",
                     template_key: Optional[str] = None,
                     order: Optional[Any] = None,
                     appearance: Optional[templates.ResumeAppearance] = None
                     ) -> Tuple[bytes, Dict[str, Any]]:
    """生成 PDF 字节流，供导出与预览共用。"""
    blocks = from_structured(structured) if structured else {}
    if name_fallback and not blocks.get("name"):
        blocks["name"] = name_fallback
    return _render_pdf(blocks, template_key, order, appearance)


def _render_pdf(blocks: Dict[str, Any],
                template_key: Optional[str] = None,
                order: Optional[Any] = None,
                appearance: Optional[templates.ResumeAppearance] = None
                ) -> Tuple[bytes, Dict[str, Any]]:
    # 取版式 → 叠外观，走 templates 里的统一入口；
    # apply_appearance 返回副本，所以这里的缩放/调优都不会污染模块级单例。
    tpl = templates.resolve_template(template_key, appearance)
    font_path, font_index, _label = find_cjk_font(prefer=tpl.pdf_font_cn)
    if not font_path:
        raise RuntimeError("系统中找不到可用的中文字体（微软雅黑/黑体/宋体），无法生成 PDF")
    font = TrueTypeFont(font_path, font_index)

    # 试几档缩放：内容超页时收一点，内容偏少时最多放大到 1.10（再大字号就不专业了）
    attempts: List[tuple] = []
    scale = 1.0
    for _ in range(5):
        engine = _TextEngine(font)
        layout = _render(tpl, blocks, engine, PAGE_W_PT, PAGE_H_PT, "", scale,
                         order=order)
        ratio = layout["used_height"] / max(1.0, layout["available_height"])
        attempts.append((scale, engine, layout, ratio))
        if layout["overflow"]:
            scale *= 0.94
            continue
        if ratio < 0.72 and scale < 1.10:
            scale = min(1.10, scale * (0.86 / max(ratio, 0.25)) ** 0.5)
            continue
        break
    safe = [a for a in attempts if not a[2]["overflow"]] or attempts
    scale, engine, layout, ratio = max(safe, key=lambda a: a[3])

    available = layout["available_height"]

    def trial(spread: float = 0.0, lead: float = 1.0, pad: float = 0.0):
        trial_engine = _TextEngine(font)
        trial_layout = _render(tpl, blocks, trial_engine, PAGE_W_PT, PAGE_H_PT,
                               "", scale, spread, lead, pad, order=order)
        return (trial_engine, trial_layout,
                trial_layout["used_height"] / max(1.0, available))

    # ---- 铺满二级：均匀放宽行距（二分逼近，不过目标值也不溢出）----
    if not layout["overflow"] and ratio < _TARGET_FILL:
        lo, hi = 1.0, min(_MAX_LEAD, 1.0 + (_TARGET_FILL - ratio) / max(ratio, 0.1))
        for _ in range(6):
            mid = (lo + hi) / 2.0
            te, tl, tr = trial(0.0, mid, 0.0)
            if tl["overflow"]:
                hi = mid
            else:
                lo = mid
                engine, layout, ratio = te, tl, tr

    # ---- 铺满三级：撑开模块间距 ----
    if not layout["overflow"] and ratio < _TARGET_FILL and layout["gap_count"]:
        need = available * (_TARGET_FILL - ratio)
        lo, hi = 0.0, min(_MAX_SPREAD, need / layout["gap_count"])
        for _ in range(6):
            mid = (lo + hi) / 2.0
            te, tl, tr = trial(mid, layout["lead"], 0.0)
            if tl["overflow"]:
                hi = mid
            else:
                lo = mid
                engine, layout, ratio = te, tl, tr

    # ---- 铺满四级：仍有富余就把留白上下平分，避免底部出现一个洞 ----
    if not layout["overflow"] and ratio < _TARGET_FILL:
        slack = available * (_TARGET_FILL - ratio)
        pad = min(_TOP_PAD_CAP, slack * 0.5)
        while pad > 1.0:
            te, tl, tr = trial(layout["spread"], layout["lead"], pad)
            if tl["overflow"]:
                pad *= 0.5
                continue
            engine, layout, ratio = te, tl, tr
            break

    layout["fill_ratio"] = round(ratio, 3)
    layout["scale"] = round(scale, 3)

    # 按真实用到的字符做字体子集，再编码内容流
    font_binary, subset_map, inverse = font.build_subset(engine.used_text())
    content = engine.build_content(subset_map)

    writer = _PdfWriter()
    catalog_num = writer.add(b"")                       # 占位，最后回填
    pages_num = writer.add(b"")
    content_num = writer.add_stream(content)
    page_num = writer.add(
        (f"<< /Type /Page /Parent {pages_num} 0 R /MediaBox [0 0 "
         f"{PAGE_W_PT:.2f} {PAGE_H_PT:.2f}] "
         f"/Resources << /Font << /F1 {5} 0 R >> >> /Contents {content_num} 0 R >>")
        .encode("latin-1"))
    # 对象 5~9：Type0 字体、CIDFont、FontDescriptor、FontFile2、ToUnicode
    ascender = font.ascender * 1000 // font.units_per_em
    descender = font.descender * 1000 // font.units_per_em
    ps_name = "SUBSET+" + _font_postscript_name(tpl.pdf_font_cn)
    writer.add(
        (f"<< /Type /Font /Subtype /Type0 /BaseFont /{ps_name} "
         f"/Encoding /Identity-H /DescendantFonts [6 0 R] /ToUnicode 9 0 R >>")
        .encode("latin-1"))
    widths = []
    for gid in sorted(inverse.keys()):
        if gid == 0:
            continue
        widths.append(font.advance_width(font.glyph_id(inverse[gid])) * 1000 // font.units_per_em)
    writer.add(
        (f"<< /Type /Font /Subtype /CIDFontType2 /BaseFont /{ps_name} "
         f"/CIDSystemInfo << /Registry (Adobe) /Ordering (Identity) /Supplement 0 >> "
         f"/FontDescriptor 7 0 R /DW 1000 /W [1 [{' '.join(str(w) for w in widths)}]] "
         f"/CIDToGIDMap /Identity >>").encode("latin-1"))
    writer.add(
        (f"<< /Type /FontDescriptor /FontName /{ps_name} /Flags 4 "
         f"/FontBBox [0 {descender} 1000 {ascender}] /ItalicAngle 0 "
         f"/Ascent {ascender} /Descent {descender} /CapHeight {ascender} "
         f"/StemV 80 /FontFile2 8 0 R >>").encode("latin-1"))
    writer.add_stream(font_binary, extra=f"/Length1 {len(font_binary)}")
    writer.add_stream(_to_unicode_cmap(inverse), compress=True)

    writer.bodies[0] = f"<< /Type /Catalog /Pages {pages_num} 0 R >>".encode("latin-1")
    writer.bodies[1] = (f"<< /Type /Pages /Kids [{page_num} 0 R] /Count 1 >>"
                        .encode("latin-1"))
    return writer.build(catalog_num), layout


def export_pdf_from_blocks(blocks: Dict[str, Any], filepath: str,
                           template_key: Optional[str] = None,
                           order: Optional[Any] = None,
                           appearance: Optional[templates.ResumeAppearance] = None
                           ) -> str:
    payload, _layout = render_pdf_blocks(blocks, template_key, order, appearance)
    with open(filepath, "wb") as fh:
        fh.write(payload)
    return filepath


def export_pdf_from_structured(structured: Dict[str, Any], filepath: str,
                               template_key: Optional[str] = None,
                               order: Optional[Any] = None,
                               appearance: Optional[templates.ResumeAppearance] = None
                               ) -> str:
    payload, _layout = render_pdf_bytes(structured,
                                        name_fallback=str(structured.get("name") or ""),
                                        template_key=template_key,
                                        order=order,
                                        appearance=appearance)
    with open(filepath, "wb") as fh:
        fh.write(payload)
    return filepath


def export_pdf(resume_text: str, filepath: str, name: str = "",
               structured: Optional[Dict[str, Any]] = None,
               template_key: Optional[str] = None,
               order: Optional[Any] = None,
               appearance: Optional[templates.ResumeAppearance] = None) -> str:
    """导出 PDF。有结构化数据直出，否则解析纯文本。"""
    if isinstance(structured, dict) and structured:
        return export_pdf_from_structured(structured, filepath, template_key,
                                          order, appearance)
    blocks = parse_resume_text(resume_text)
    if name and not blocks.get("name"):
        blocks["name"] = name
    return export_pdf_from_blocks(blocks, filepath, template_key, order, appearance)


def default_pdf_path(directory: str, stem: str) -> str:
    os.makedirs(directory, exist_ok=True)
    safe = re.sub(r'[\\/:*?"<>|]', "_", stem or "简历").strip() or "简历"
    return os.path.join(directory, f"{safe}.pdf")
