"""本地 OCR（Windows 10+ 内置 OCR 引擎，简体中文）。

依赖 winrt（pywinrt 系列包），未安装 / 中文语言包缺失时优雅降级返回空串，
调用方据此提示用户「手动粘贴文字」。打包 EXE 时无需携带 tesseract 二进制，体积小。

引擎行为与对策（2026-09-30 真机实测）：
系统 OCR 会在**汉字之间插入空格**（"招聘 Python 后端工程师" →
「招 聘 Python 后 端 工 程 师」），且 `result.text` 会把多行用空格拼成一行。
故统一走 clean_ocr_text() 清理、并从 result.lines 逐行取文本保留换行。
"""
from __future__ import annotations

import re
from typing import List

# CJK 标点(U+3000-303F) + 汉字(U+4E00-9FFF) + 全角形式(U+FF00-FFEF)
_CJK = r"\u3000-\u303f\u4e00-\u9fff\uff00-\uffef"
# 两侧都是中文时，中间那个空格是引擎硬塞的，删掉；
# 中文与英文/数字之间、纯英文/数字之间的空格保留——否则 "招聘 Python" 会粘成 "招聘Python"。
_SPACE_BETWEEN_CJK = re.compile(rf"(?<=[{_CJK}])[ \t]+(?=[{_CJK}])")
_MULTI_SPACE = re.compile(r"[ \t]{2,}")


def clean_ocr_text(text: str) -> str:
    """清理系统 OCR 输出的中文排版噪音。

    1) 删除汉字 / 中文标点之间的空格（引擎会逐字加空格）；
    2) 压缩连续空格为单个；
    3) 每行 strip 去掉行首尾空白与整体空行。
    """
    text = _SPACE_BETWEEN_CJK.sub("", text or "")
    text = _MULTI_SPACE.sub(" ", text)
    return "\n".join(ln.strip() for ln in text.splitlines()).strip()


def is_ocr_available() -> bool:
    """本机是否可用**简体中文** Windows OCR（winrt 已装且中文引擎可用）。

    必须是真正存在 zh-Hans-CN 引擎才算可用：旧逻辑「zh 引擎 或 用户配置语言引擎
    任一非空」在本机未装中文语言包时会回退到英文引擎，导致中文识别全错
    （实测返回空串）——属假阳性，故收紧为严格判定。
    """
    try:
        from winrt.windows.media.ocr import OcrEngine
        from winrt.windows.globalization import Language

        if OcrEngine.try_create_from_language(Language("zh-Hans-CN")) is None:
            return False
        return any(
            lang.language_tag == "zh-Hans-CN"
            for lang in OcrEngine.available_recognizer_languages
        )
    except Exception:  # noqa: BLE001
        return False


async def _recognize_async(path: str) -> str:
    from winrt.windows.media.ocr import OcrEngine
    from winrt.windows.globalization import Language
    from winrt.windows.graphics.imaging import BitmapDecoder
    from winrt.windows.storage import StorageFile, FileAccessMode

    engine = OcrEngine.try_create_from_language(Language("zh-Hans-CN"))
    if engine is None:
        return ""

    file = await StorageFile.get_file_from_path_async(path)
    stream = await file.open_async(FileAccessMode.READ)
    decoder = await BitmapDecoder.create_async(stream)
    bitmap = await decoder.get_software_bitmap_async()
    result = await engine.recognize_async(bitmap)
    # 用 lines 逐行拼接以保留换行（result.text 会把各行用空格拼成一行）
    return clean_ocr_text("\n".join(line.text for line in result.lines))


def ocr_image_file(path: str) -> str:
    """识别图片文字；不可用/失败返回空串（不抛异常）。

    winrt 的 StorageFile 对中文/非 ASCII 路径有 bug（UNABLE_TO_MASK_PATH），
    故先复制到 ASCII 临时路径再识别。
    """
    import os
    import shutil
    import tempfile
    import uuid

    tmp = os.path.join(tempfile.gettempdir(), f"offerflow_ocr_{uuid.uuid4().hex}.png")
    try:
        shutil.copy2(path, tmp)
        import asyncio
        return asyncio.run(_recognize_async(tmp))
    except Exception:  # noqa: BLE001
        return ""
    finally:
        try:
            os.remove(tmp)
        except Exception:  # noqa: BLE001
            pass


def ocr_lines(path: str) -> List[str]:
    """返回逐行文本（空行已过滤）。"""
    text = ocr_image_file(path)
    return [ln.strip() for ln in text.splitlines() if ln.strip()]
