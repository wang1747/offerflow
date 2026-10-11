"""
OCR 服务：图片文字识别

提供两条路径：
1. 视觉大模型（vision_service，优先）——理解版式，复杂 JD 截图效果好
2. 本地 pytesseract（降级兜底）——视觉不可用/失败/返回为空时启用

对外统一入口：extract_text_smart()
"""

import io
import re
from typing import Optional, Tuple

import pytesseract
from PIL import Image


def extract_text_from_image(image_bytes: bytes) -> str:
    """
    从图片中提取文字
    支持：PNG, JPG, JPEG, BMP, WEBP
    """
    try:
        image = Image.open(io.BytesIO(image_bytes))
        # 预处理：灰度化
        if image.mode != "L":
            image = image.convert("L")
        # 使用中文 + 英文语言包
        text = pytesseract.image_to_string(image, lang="chi_sim+eng")
        # 清洗多余空白
        text = re.sub(r"\n\s*\n", "\n\n", text.strip())
        return text
    except Exception as e:
        raise ValueError(f"OCR 识别失败: {e}")


def extract_text_from_image_file(file_bytes: bytes, filename: str) -> str:
    """
    从上传的图片文件中提取文字（本地 OCR，作为降级路径保留）
    """
    supported_extensions = [".png", ".jpg", ".jpeg", ".bmp", ".webp", ".gif"]
    ext = filename.lower()
    if not any(ext.endswith(suffix) for suffix in supported_extensions):
        raise ValueError(f"不支持的图片格式: {filename}，支持: {', '.join(supported_extensions)}")

    return extract_text_from_image(file_bytes)


def extract_text_smart(
    image_bytes: bytes,
    filename: str,
    user=None,
    llm=None,
    min_chars: int = 10,
) -> Tuple[str, str]:
    """
    统一入口：**视觉大模型优先，本地 OCR 降级**。

    返回 (文字, 引擎名)，引擎名为 "vision" 或 "ocr"。

    降级条件（任一满足即回退 OCR）：
      - 未提供 user/llm（无法调用模型，如未登录场景）
      - 视觉调用抛异常
      - 视觉返回空或文字过短（< min_chars）

    两条路径都失败时抛 ValueError，由调用方转成 400 提示。
    """
    # ---- 1. 视觉优先 ----
    if user is not None or llm is not None:
        try:
            from app.modules.jd.vision_service import extract_text_from_image_vision

            text = extract_text_from_image_vision(
                image_bytes, filename=filename, user=user, llm=llm
            )
            if text and len(text.strip()) >= min_chars:
                return text.strip(), "vision"
        except Exception:
            pass  # 任何异常都静默降级

    # ---- 2. 本地 OCR 降级 ----
    try:
        text = extract_text_from_image_file(image_bytes, filename)
    except ValueError:
        raise  # 格式不支持等，直接透出给调用方
    except Exception as e:  # noqa: BLE001
        raise ValueError(f"OCR 识别失败: {e}")

    if not text or len(text.strip()) < min_chars:
        raise ValueError("图片文字识别结果为空，请确保图片清晰包含文字")

    return text.strip(), "ocr"