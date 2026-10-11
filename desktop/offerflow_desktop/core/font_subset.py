"""TrueType 字体解析与子集化（纯标准库，用于把中文字体内嵌进 PDF）。

为什么需要它：PDF 要正确显示中文，必须内嵌字体；而系统中文字体动辄 9~19MB，
整份嵌入会让简历 PDF 大到不可发送。本模块只抽取实际用到的字形，通常能把
内嵌体积压到 100KB 量级。

只支持 TrueType 轮廓（glyf/loca）——系统中文字体基本都是这个格式。
"""

from __future__ import annotations

import os
import struct
from typing import Dict, List, Optional, Set, Tuple

# Windows 常见中文字体候选：(文件名, 字体序号, 显示名, 是否衬线)
FONT_CANDIDATES: List[Tuple[str, int, str, bool]] = [
    ("msyh.ttc", 0, "微软雅黑", False),
    ("msyh.ttf", 0, "微软雅黑", False),
    ("Deng.ttf", 0, "等线", False),
    ("simhei.ttf", 0, "黑体", False),
    ("simkai.ttf", 0, "楷体", True),
    ("simsun.ttc", 0, "宋体", True),
]


def default_fonts_dir() -> str:
    win = os.environ.get("WINDIR") or "C:\\Windows"
    return os.path.join(win, "Fonts")


def find_cjk_font(fonts_dir: Optional[str] = None, prefer: Optional[str] = None) -> Tuple[str, int, str]:
    """返回 (字体文件路径, 字体序号, 显示名)；找不到返回 ("", 0, "")。"""
    directory = fonts_dir or default_fonts_dir()
    candidates = FONT_CANDIDATES
    if prefer:
        ranked = [c for c in FONT_CANDIDATES if c[2] == prefer]
        ranked += [c for c in FONT_CANDIDATES if c[2] != prefer]
        candidates = ranked
    for name, index, label, _serif in candidates:
        path = os.path.join(directory, name)
        if os.path.exists(path):
            return path, index, label
    return "", 0, ""


def _checksum(data: bytes) -> int:
    if len(data) % 4:
        data = data + b"\0" * (4 - len(data) % 4)
    total = 0
    for i in range(0, len(data), 4):
        total = (total + struct.unpack(">I", data[i:i + 4])[0]) & 0xFFFFFFFF
    return total


def _search_params(num_tables: int) -> Tuple[int, int, int]:
    power = 1
    while power * 2 <= num_tables:
        power *= 2
    search_range = power * 16
    entry_selector = power.bit_length() - 1
    range_shift = num_tables * 16 - search_range
    return search_range, entry_selector, range_shift


class TrueTypeFont:
    """解析 TrueType / TrueType Collection，并支持按字符子集化。"""

    def __init__(self, path: str, font_index: int = 0):
        with open(path, "rb") as fh:
            self.data = fh.read()
        self.path = path
        self.font_index = font_index
        self.tables: Dict[str, Tuple[int, int]] = {}
        self._read_table_directory()
        self._read_head()
        self._read_hhea()
        self._read_maxp()
        self._read_cmap()
        self._read_loca()

    # ---------- 表目录 ----------

    def _read_table_directory(self) -> None:
        data = self.data
        if data[:4] == b"ttcf":
            num_fonts = struct.unpack(">I", data[8:12])[0]
            if self.font_index >= num_fonts:
                raise ValueError(f"字体集合只有 {num_fonts} 个字体，序号 {self.font_index} 越界")
            offsets = struct.unpack(f">{num_fonts}I", data[12:12 + 4 * num_fonts])
            base = offsets[self.font_index]
        else:
            base = 0
        num_tables = struct.unpack(">H", data[base + 4:base + 6])[0]
        for i in range(num_tables):
            off = base + 12 + 16 * i
            tag, _cks, toff, tlen = struct.unpack(">4sIII", data[off:off + 16])
            self.tables[tag.decode("latin1")] = (toff, tlen)

    def has(self, tag: str) -> bool:
        return tag in self.tables

    def table(self, tag: str) -> bytes:
        off, length = self.tables[tag]
        return self.data[off:off + length]

    # ---------- 关键表 ----------

    def _read_head(self) -> None:
        head = self.table("head")
        self.units_per_em = struct.unpack(">H", head[18:20])[0]
        self.index_to_loc_format = struct.unpack(">h", head[50:52])[0]
        self.x_min, self.y_min, self.x_max, self.y_max = struct.unpack(">4h", head[36:44])

    def _read_hhea(self) -> None:
        hhea = self.table("hhea")
        self.ascender = struct.unpack(">h", hhea[4:6])[0]
        self.descender = struct.unpack(">h", hhea[6:8])[0]
        self.num_h_metrics = struct.unpack(">H", hhea[34:36])[0]

    def _read_maxp(self) -> None:
        maxp = self.table("maxp")
        self.num_glyphs = struct.unpack(">H", maxp[4:6])[0]

    def _read_cmap(self) -> None:
        """建立 unicode -> glyph id 映射（优先 format 12，其次 format 4）。

        子集字体可以不带 cmap（PDF 走 Identity-H 编码 + ToUnicode，不依赖它），
        所以要容忍缺失，否则解析自己生成的子集字体就会失败。
        """
        self._cmap: Dict[int, int] = {}
        if not self.has("cmap"):
            return
        cmap = self.table("cmap")
        num_tables = struct.unpack(">H", cmap[2:4])[0]
        best: Optional[Tuple[int, int, int]] = None   # (优先级, offset, format)
        for i in range(num_tables):
            off = 4 + 8 * i
            platform, encoding, sub_off = struct.unpack(">HHI", cmap[off:off + 8])
            fmt = struct.unpack(">H", cmap[sub_off:sub_off + 2])[0]
            priority = 0
            if platform == 3 and encoding == 10 and fmt == 12:
                priority = 4
            elif platform == 0 and fmt == 12:
                priority = 3
            elif platform == 3 and encoding == 1 and fmt == 4:
                priority = 2
            elif platform == 0 and fmt == 4:
                priority = 1
            if priority and (best is None or priority > best[0]):
                best = (priority, sub_off, fmt)
        if best is None:
            return
        _priority, sub_off, fmt = best
        if fmt == 12:
            self._parse_cmap_12(cmap, sub_off)
        else:
            self._parse_cmap_4(cmap, sub_off)

    def _parse_cmap_12(self, cmap: bytes, off: int) -> None:
        n_groups = struct.unpack(">I", cmap[off + 12:off + 16])[0]
        pos = off + 16
        for _ in range(n_groups):
            start, end, start_gid = struct.unpack(">III", cmap[pos:pos + 12])
            pos += 12
            if end - start > 0x20000:      # 防御：异常表
                continue
            for code in range(start, end + 1):
                self._cmap[code] = start_gid + (code - start)

    def _parse_cmap_4(self, cmap: bytes, off: int) -> None:
        seg_count = struct.unpack(">H", cmap[off + 6:off + 8])[0] // 2
        end_pos = off + 14
        start_pos = end_pos + seg_count * 2 + 2
        delta_pos = start_pos + seg_count * 2
        range_pos = delta_pos + seg_count * 2
        for i in range(seg_count):
            end_code = struct.unpack(">H", cmap[end_pos + i * 2:end_pos + i * 2 + 2])[0]
            start_code = struct.unpack(">H", cmap[start_pos + i * 2:start_pos + i * 2 + 2])[0]
            id_delta = struct.unpack(">h", cmap[delta_pos + i * 2:delta_pos + i * 2 + 2])[0]
            id_range_offset = struct.unpack(">H", cmap[range_pos + i * 2:range_pos + i * 2 + 2])[0]
            if start_code == 0xFFFF:
                continue
            for code in range(start_code, end_code + 1):
                if id_range_offset == 0:
                    gid = (code + id_delta) & 0xFFFF
                else:
                    gi = range_pos + i * 2 + id_range_offset + (code - start_code) * 2
                    if gi + 2 > len(cmap):
                        continue
                    gid = struct.unpack(">H", cmap[gi:gi + 2])[0]
                    if gid:
                        gid = (gid + id_delta) & 0xFFFF
                if gid:
                    self._cmap[code] = gid

    def _read_loca(self) -> None:
        loca = self.table("loca")
        count = self.num_glyphs + 1
        if self.index_to_loc_format == 0:
            self.loca = [v * 2 for v in struct.unpack(f">{count}H", loca[:count * 2])]
        else:
            self.loca = list(struct.unpack(f">{count}I", loca[:count * 4]))

    # ---------- 公开查询 ----------

    def glyph_id(self, code: int) -> int:
        return self._cmap.get(code, 0)

    def glyph_ids_for(self, text: str) -> Dict[int, int]:
        """返回 {unicode 码点: glyph id}（只含能映射到的）。"""
        result: Dict[int, int] = {}
        for ch in text:
            code = ord(ch)
            gid = self._cmap.get(code)
            if gid:
                result[code] = gid
        return result

    def advance_width(self, gid: int) -> int:
        """字形宽度（字体单位）。"""
        hmtx = self.table("hmtx")
        if gid < self.num_h_metrics:
            return struct.unpack(">H", hmtx[gid * 4:gid * 4 + 2])[0]
        last = self.num_h_metrics - 1
        return struct.unpack(">H", hmtx[last * 4:last * 4 + 2])[0]

    def text_width_units(self, text: str) -> int:
        """文本宽度（字体单位），用于排版换行与对齐。"""
        total = 0
        for ch in text:
            gid = self._cmap.get(ord(ch), 0)
            total += self.advance_width(gid) if gid else self.advance_width(0)
        return total

    # ---------- 子集化 ----------

    def _composite_components(self, glyph: bytes) -> List[int]:
        """解析复合字形引用的其他 glyph id。"""
        if len(glyph) < 10:
            return []
        num_contours = struct.unpack(">h", glyph[0:2])[0]
        if num_contours >= 0:
            return []
        component_ids: List[int] = []
        pos = 10
        while pos + 4 <= len(glyph):
            flags, index = struct.unpack(">HH", glyph[pos:pos + 4])
            pos += 4
            component_ids.append(index)
            pos += 4 if flags & 0x0001 else 2          # ARG_1_AND_2_ARE_WORDS
            if flags & 0x0008:                          # WE_HAVE_A_SCALE
                pos += 2
            elif flags & 0x0040:                        # WE_HAVE_AN_X_AND_Y_SCALE
                pos += 4
            elif flags & 0x0080:                        # WE_HAVE_A_TWO_BY_TWO
                pos += 8
            if not flags & 0x0020:                      # MORE_COMPONENTS
                break
        return component_ids

    def _expand_glyphs(self, glyphs: Set[int]) -> Set[int]:
        """把复合字形依赖的字形也纳入集合。"""
        all_glyphs = set(glyphs)
        pending = list(glyphs)
        while pending:
            gid = pending.pop()
            if gid + 1 >= len(self.loca):
                continue
            start, end = self.loca[gid], self.loca[gid + 1]
            if end <= start:
                continue
            glyph = self.glyf_data[start:end]
            for comp in self._composite_components(glyph):
                if comp not in all_glyphs:
                    all_glyphs.add(comp)
                    pending.append(comp)
        return all_glyphs

    @property
    def glyf_data(self) -> bytes:
        if not hasattr(self, "_glyf"):
            self._glyf = self.table("glyf")
        return self._glyf

    def build_subset(self, text: str) -> Tuple[bytes, Dict[int, int], Dict[int, int]]:
        """生成子集字体。

        返回 (字体二进制, {unicode: 新 glyph id}, {新 glyph id: unicode})。
        字形 0 始终保留，新编号从 1 开始。
        """
        mapping = {}
        for code, gid in self.glyph_ids_for(text).items():
            mapping[code] = gid
        needed = self._expand_glyphs(set(mapping.values()))

        # 新编号：0=.notdef 保留，其余按原 gid 升序
        order = sorted(needed)
        old_to_new: Dict[int, int] = {0: 0}
        for i, gid in enumerate(order, start=1):
            old_to_new[gid] = i
        new_to_old = [0] + order

        # 重建 glyf / loca
        glyf_parts: List[bytes] = []
        loca: List[int] = []
        offset = 0
        # glyph 0：通常为空
        loca.append(offset)
        for new_gid in range(1, len(new_to_old)):
            old_gid = new_to_old[new_gid]
            loca.append(offset)
            if old_gid + 1 >= len(self.loca):
                continue
            start, end = self.loca[old_gid], self.loca[old_gid + 1]
            if end <= start:
                continue
            glyph = bytearray(self.glyf_data[start:end])
            if len(glyph) >= 2 and struct.unpack(">h", glyph[0:2])[0] < 0:
                self._remap_composites(glyph, old_to_new)
            if len(glyph) % 4:
                glyph += b"\0" * (4 - len(glyph) % 4)
            glyf_parts.append(bytes(glyph))
            offset += len(glyph)
        loca.append(offset)
        glyf = b"".join(glyf_parts)

        # 新 hmtx（每字形一条 long metric）
        hmtx_parts = []
        for new_gid in range(len(new_to_old)):
            gid = new_to_old[new_gid]
            hmtx_parts.append(struct.pack(">Hh", self.advance_width(gid), 0))
        hmtx = b"".join(hmtx_parts)

        # 新 loca（统一用 long 格式）
        loca_bytes = struct.pack(f">{len(loca)}I", *loca)

        num_glyphs = len(new_to_old)
        maxp = bytearray(self.table("maxp"))
        maxp[4:6] = struct.pack(">H", num_glyphs)

        head = bytearray(self.table("head"))
        head[50:52] = struct.pack(">h", 1)             # indexToLocFormat = long
        head[8:12] = struct.pack(">I", 0)              # checkSumAdjustment 稍后回填

        hhea = bytearray(self.table("hhea"))
        hhea[34:36] = struct.pack(">H", num_glyphs)    # numberOfHMetrics

        new_tables: Dict[str, bytes] = {
            "head": bytes(head),
            "hhea": bytes(hhea),
            "maxp": bytes(maxp),
            "hmtx": hmtx,
            "loca": loca_bytes,
            "glyf": glyf,
        }
        for tag in ("cvt ", "fpgm", "prep", "gasp"):
            if self.has(tag):
                new_tables[tag] = self.table(tag)

        font_binary = self._assemble(new_tables)

        new_mapping = {code: old_to_new[gid] for code, gid in mapping.items()}
        inverse = {v: k for k, v in new_mapping.items()}
        self._subset_tables = new_tables
        self._glyph_order = new_to_old
        return font_binary, new_mapping, inverse

    def _remap_composites(self, glyph: bytearray, old_to_new: Dict[int, int]) -> None:
        """把复合字形里引用的字形编号改成子集后的新编号。"""
        pos = 10
        while pos + 4 <= len(glyph):
            flags, index = struct.unpack(">HH", glyph[pos:pos + 4])
            new_index = old_to_new.get(index)
            if new_index is None:
                new_index = 0
            glyph[pos + 2:pos + 4] = struct.pack(">H", new_index)
            pos += 4
            pos += 4 if flags & 0x0001 else 2
            if flags & 0x0008:
                pos += 2
            elif flags & 0x0040:
                pos += 4
            elif flags & 0x0080:
                pos += 8
            if not flags & 0x0020:
                break

    def _assemble(self, tables: Dict[str, bytes]) -> bytes:
        """按 sfnt 规范拼装字体文件（表目录 + 表数据，各自 4 字节对齐）。"""
        tags = sorted(tables.keys())
        num_tables = len(tags)
        search_range, entry_selector, range_shift = _search_params(num_tables)

        header = struct.pack(">IHHHH", 0x00010000, num_tables,
                             search_range, entry_selector, range_shift)
        directory = bytearray()
        body = bytearray()
        offset = 12 + 16 * num_tables

        for tag in tags:
            payload = tables[tag]
            directory += struct.pack(">4sIII", tag.encode("latin1"),
                                     _checksum(payload), offset, len(payload))
            body += payload
            padding = (4 - len(payload) % 4) % 4
            body += b"\0" * padding
            offset += len(payload) + padding

        font = header + bytes(directory) + bytes(body)

        # 回填 head.checkSumAdjustment = 0xB1B0AFBA - 整字体校验和
        head_offset = None
        for i, tag in enumerate(tags):
            if tag == "head":
                head_offset = 12 + 16 * i + 8          # 目录项里的表偏移字段
                break
        if head_offset is not None:
            data_offset = struct.unpack(">I", font[head_offset:head_offset + 4])[0]
            adjustment = (0xB1B0AFBA - _checksum(font)) & 0xFFFFFFFF
            font = bytearray(font)
            font[data_offset + 8:data_offset + 12] = struct.pack(">I", adjustment)
            font = bytes(font)
        return font
