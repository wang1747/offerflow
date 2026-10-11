# -*- coding: utf-8 -*-
"""导出后回读 PDF 自检（2026-10-07）。

## 为什么需要它

我们对自己的 PDF 导出做过两件"看不见的"改造：

  * **中文字体子集化**（``core/font_subset.py``）—— 只把用到的字形打进 PDF。
    子集化算错一个字，那个字就**静默消失**：文件照样能打开、缩略图照样好看。
  * **一页调优**（scale / lead / spread / top_pad 四段式）—— 靠收紧行距与
    整体缩放把内容压进一页。压过头时最靠后的段落会被**截断**，而
    "少了一行"在预览图上几乎看不出来。

这两类缺陷的共同点是：**离线读代码永远发现不了**。唯一可靠的判据是把刚
导出的 PDF 重新读回来，跟"我本来写了什么"对一遍。这就是本模块。

## 判据（可回验，不是"看着对不对"）

  1. **锚点必须都在**：结构化数据里存在的短锚点（姓名、每段经历标题、
     技能项、证书项、手机号、邮箱）在回读文本里要能逐字找到。
  2. **两档诊断**：整串找不到、但前缀（≥ ``_PREFIX_MIN`` 字）找得到 ⇒
     报「截断」；连前缀都找不到 ⇒ 报「缺失」。分开报的必要性在于修法
     完全不同 —— 截断＝排版压过头，缺失＝字体子集/编码问题。
  3. **覆盖率**：回读字符数 / 期望字符数 ≥ ``MIN_COVERAGE``。
  4. **空回读**：引擎可用、页数 > 0、却一个字都读不出来 ⇒ **这是真缺陷**
     （字形被转成图形或字体没嵌入），不能当环境问题放过。

## 不许把环境故障报成内容缺陷

解析引擎不可用时 ``engine`` 为空 ⇒ 返回 ``degraded=True`` 且 ``ok=True``，
并附一句说明。**回读不了不等于内容有问题**（错题本里"环境假故障"那一类
教训：判据必须自证，别把环境问题算到被测对象头上）。
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Tuple

# 回读字符数 / 期望字符数的下限。留 15% 余量是因为：
# 分隔线、项目符号、图标、页眉页码这些"装饰性"字符也会被回读出来，
# 而我们在期望侧只统计正文，两边天然对不齐。
MIN_COVERAGE = 0.85

# 锚点前缀匹配的最小长度。低于这个长度（比如"张"、"Python"里的 "Py"）
# 前缀匹配会变成噪声：几乎任何 PDF 都能凑巧命中。
_PREFIX_MIN = 6

# 参与锚点比对的正文分区。与 ``core/sections.BODY_SECTIONS`` 同源，
# 但这里只关心"能当锚点用"的字段，所以单独列。
_SKIP_ANCHOR_LEN = 2


@dataclass
class PdfVerifyResult:
    """一次回读自检的结果。"""

    ok: bool = True
    pages: int = 0
    engine: str = ""
    read_chars: int = 0
    expect_chars: int = 0
    coverage: float = 0.0
    missing: List[str] = field(default_factory=list)     # 连前缀都找不到
    truncated: List[str] = field(default_factory=list)   # 前缀在、整串不在
    degraded: bool = False        # 回读本身没跑成（引擎缺失等），不是内容缺陷
    note: str = ""

    @property
    def checked(self) -> bool:
        """这次到底有没有真的比过内容。"""
        return not self.degraded and self.engine != ""

    def summary(self) -> str:
        """给 UI 用的一句话结论。"""
        if self.degraded:
            return f"未能回读校验：{self.note or '本地解析引擎不可用'}"
        if not self.checked:
            return "未做回读校验"
        bits = [f"{self.pages} 页 · 回读 {self.read_chars} 字（覆盖率 {self.coverage:.0%}）"]
        if self.missing:
            bits.append(f"{len(self.missing)} 处内容在 PDF 里找不到")
        if self.truncated:
            bits.append(f"{len(self.truncated)} 处疑似被截断")
        if self.ok and not self.missing and not self.truncated:
            bits.insert(0, "导出内容完整")
        return "；".join(bits)

    def details(self, limit: int = 4) -> List[str]:
        """给 UI 用的明细行（截断与缺失分开列）。"""
        out: List[str] = []
        for item in self.truncated[:limit]:
            out.append(f"疑似被截断：{item}")
        for item in self.missing[:limit]:
            out.append(f"在 PDF 里找不到：{item}")
        rest = len(self.truncated) + len(self.missing) - len(out)
        if rest > 0:
            out.append(f"（另有 {rest} 处）")
        return out


# --------------------------------------------------------------------------- #
# 文本归一
# --------------------------------------------------------------------------- #

_WS_RE = re.compile(r"\s+")
# 全角标点 → 半角。PDF 排版层有时会把中文全角标点换成半角，
# 不归一的话「今天，我」与「今天,我」会被判成内容缺失。
_PUNCT_MAP = {
    "，": ",", "。": ".", "、": ",", "；": ";", "：": ":",
    "（": "(", "）": ")", "【": "[", "】": "]",
    "“": '"', "”": '"', "‘": "'", "’": "'",
    "—": "-", "–": "-", "－": "-", "～": "~", "·": "·",
}


def normalize_for_match(text: Any) -> str:
    """比对用归一：去空白 + 全角标点转半角 + NFKC。

    只做"排版层的等价变换"，不做同义替换 —— 否则判据就从
    「PDF 里有没有这几个字」漂移成「像不像」，那是另一回事。
    """
    s = str(text or "")
    if not s:
        return ""
    s = unicodedata.normalize("NFKC", s)
    s = "".join(_PUNCT_MAP.get(ch, ch) for ch in s)
    return _WS_RE.sub("", s)


def _digits(text: Any) -> str:
    """只留数字，用于手机号比对（PDF 里可能被排成 138-0000-0000）。"""
    return re.sub(r"\D", "", str(text or ""))


# --------------------------------------------------------------------------- #
# 从结构化块里取锚点
# --------------------------------------------------------------------------- #

def _iter_body_strings(blocks: Dict[str, Any]) -> Iterable[str]:
    """正文里所有该出现在 PDF 上的字符串（用于算期望字数）。"""
    blocks = blocks if isinstance(blocks, dict) else {}
    yield str(blocks.get("name") or "")
    yield str(blocks.get("contact_line") or "")
    yield str(blocks.get("position") or "")
    yield str(blocks.get("summary") or "")
    for edu in blocks.get("education") or []:
        if isinstance(edu, dict):
            yield str(edu.get("left") or "")
            yield str(edu.get("period") or "")
            for d in edu.get("details") or []:
                yield str(d or "")
    for exp in blocks.get("experiences") or []:
        if isinstance(exp, dict):
            yield str(exp.get("left") or "")
            yield str(exp.get("period") or "")
            for b in exp.get("bullets") or []:
                yield str(b or "")
    for key in ("skills", "certifications", "honors"):
        for item in blocks.get(key) or []:
            yield str(item or "")
    for extra in blocks.get("extra") or []:
        if isinstance(extra, dict):
            yield str(extra.get("title") or "")
            for line in extra.get("lines") or []:
                yield str(line or "")


def anchors_of(blocks: Dict[str, Any]) -> List[str]:
    """挑出"必须能在回读文本里找到"的锚点。

    选短锚点而不是整段：正文在 PDF 里会被重新折行，整段几乎必然对不上，
    那样报出来的全是假阳性，用户看两遍就不信这个检查了。
    """
    blocks = blocks if isinstance(blocks, dict) else {}
    out: List[str] = []

    def add(value: Any, *, limit: Optional[int] = None) -> None:
        text = normalize_for_match(value)
        if limit is not None and len(text) > limit:
            text = text[:limit]
        if len(text) >= _SKIP_ANCHOR_LEN:
            out.append(text)

    add(blocks.get("name"))
    add(blocks.get("position"))
    # 邮箱整体；手机号单独按纯数字比（排版可能插分隔符）
    contact = str(blocks.get("contact_line") or "")
    mail = re.search(r"[\w.+-]+@[\w-]+\.[\w.]+", contact)
    if mail:
        add(mail.group(0))
    phone = _digits(contact)
    if len(phone) >= 7:
        out.append("#digits:" + phone)

    for edu in blocks.get("education") or []:
        if isinstance(edu, dict):
            add(edu.get("left"))
    for exp in blocks.get("experiences") or []:
        if not isinstance(exp, dict):
            continue
        add(exp.get("left"))
        for bullet in exp.get("bullets") or []:
            # 要点取前 16 字：足够定位一句话，又不会被折行/尾字丢掉整条
            add(bullet, limit=16)
    for key in ("skills", "certifications", "honors"):
        for item in blocks.get(key) or []:
            add(item, limit=20)

    # 去重但保序（顺序稳定，界面与测试才好对）
    seen = set()
    uniq: List[str] = []
    for a in out:
        if a not in seen:
            seen.add(a)
            uniq.append(a)
    return uniq


# --------------------------------------------------------------------------- #
# 主入口
# --------------------------------------------------------------------------- #

def verify_pdf_text(read_text: str, blocks: Dict[str, Any],
                    *, pages: int = 0, engine: str = "",
                    min_coverage: float = MIN_COVERAGE
                    ) -> PdfVerifyResult:
    """把"回读到的文本"与"本该有的内容"对一遍。

    与读文件解耦，是为了能在测试里直接喂构造文本 —— 否则这条判据就只能
    依赖"本机装了哪个 PDF 引擎"，那用例就不是自洽（hermetic）的了。
    """
    expect = "".join(
        normalize_for_match(s) for s in _iter_body_strings(blocks))
    result = PdfVerifyResult(pages=int(pages or 0), engine=str(engine or ""),
                             expect_chars=len(expect))

    if not result.engine:
        result.degraded = True
        result.note = "本机没有可用的 PDF 文本解析引擎"
        return result

    read = normalize_for_match(read_text)
    result.read_chars = len(read)

    if not read:
        # 引擎可用、却读不出一个字 ⇒ 真缺陷（字形被转成图形或字体未嵌入）。
        result.ok = False
        result.note = ("回读不到任何文字：PDF 里的字形可能被转成了图形，"
                       "或字体未正确嵌入，招聘系统同样读不到")
        return result

    read_digits = _digits(read)
    for anchor in anchors_of(blocks):
        if anchor.startswith("#digits:"):
            if anchor[len("#digits:"):] not in read_digits:
                result.missing.append("手机号")
            continue
        if anchor in read:
            continue
        # 前缀在、整串不在 ⇒ 尾部被吃掉，典型是排版压过头截断
        head = anchor[:_PREFIX_MIN]
        if len(anchor) > _PREFIX_MIN and head in read:
            result.truncated.append(anchor)
        else:
            result.missing.append(anchor)

    if result.expect_chars:
        result.coverage = min(1.0, result.read_chars / float(result.expect_chars))

    # 覆盖率只在"字符明显偏少"时才判失败：正常情况回读会**多**出分隔符，
    # 偏多不算问题，偏少才是丢内容。
    low_coverage = bool(result.expect_chars) and result.coverage < min_coverage
    result.ok = not (result.missing or result.truncated or low_coverage)
    if low_coverage and not result.missing:
        result.note = (f"回读字符数只有期望的 {result.coverage:.0%}，"
                       "内容可能被大幅截断或挤压")
    return result


def verify_pdf_file(path: str, blocks: Dict[str, Any],
                    *, min_coverage: float = MIN_COVERAGE) -> PdfVerifyResult:
    """读回一个真实的 PDF 文件做自检。

    只在"确实生成了文件"之后调用。解析失败 ⇒ degraded，**不阻断导出**：
    自检是附加保障，不能反过来把导出这件事弄失败。
    """
    try:
        from ..local_ai import parse as _parse
    except Exception:                                     # pragma: no cover
        return PdfVerifyResult(degraded=True, note="本地解析模块不可用")

    try:
        status = _parse.parse_status() or {}
    except Exception:                                     # pragma: no cover
        status = {}
    pdf_engine = bool(status.get("liteparse") or status.get("pypdf"))
    if not pdf_engine:
        return PdfVerifyResult(degraded=True, note="本机没有可用的 PDF 文本解析引擎")

    try:
        parsed = _parse.parse_pdf(str(path))
    except Exception as exc:                              # noqa: BLE001
        return PdfVerifyResult(degraded=True, note=f"回读失败：{exc}")

    return verify_pdf_text(getattr(parsed, "text", "") or "", blocks,
                           pages=int(getattr(parsed, "pages", 0) or 0),
                           engine=str(getattr(parsed, "engine", "") or ""),
                           min_coverage=min_coverage)


def verify_exported_pdf(path: str,
                        structured: Dict[str, Any]) -> PdfVerifyResult:
    """导出流程专用封装：直接吃 LLM 输出的 ``structured``。

    页面只需要传「刚写出去的文件」和「本来是照着什么写的」，块转换与
    异常兜底都在这里做掉 —— 两个导出页面的调用点就不会各写一套、
    也不会各自漏一个 try。
    """
    try:
        from .resume_review import from_structured
        blocks = from_structured(structured if isinstance(structured, dict) else {})
    except Exception as exc:                              # noqa: BLE001
        return PdfVerifyResult(degraded=True, note=f"无法取得期望内容：{exc}")
    return verify_pdf_file(path, blocks)
