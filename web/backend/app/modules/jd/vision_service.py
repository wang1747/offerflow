"""
视觉识别服务：用多模态大模型直接从图片提取文字（优先于本地 OCR）

设计要点：
- 只做「图 -> 文字」，产出 raw_text，下游解析链路完全不变（语料/检索/analyze_jd 都统一）。
- 模型为推理模型（返回可能含 reasoning_content），**max_tokens 必须给足**，
  否则推理 token 吃光预算会导致 content 为空（实测 max_tokens=60 时回答为空）。
- 图片格式规范化：视觉接口仅接受 webp/png/jpeg/gif，**bmp 会自动转 png**；过大图片压缩。
- 任何异常都静默返回 ""（由调用方降级到本地 OCR），绝不阻断主流程。
"""

from __future__ import annotations

import base64
import io
import re
from typing import Optional

from app.core.llm import get_user_llm_or_raise
from app.core.prompt_guards import INJECTION_GUARD, attach
from app.models.user import User

# 视觉接口支持的格式（实测报错原文：webp, png, jpeg, gif）
_VISION_OK_EXT = [".png", ".jpg", ".jpeg", ".webp", ".gif"]
_SUPPORTED_EXT = _VISION_OK_EXT + [".bmp"]

# 图片压缩上限：长边像素 + 输出质量
_MAX_SIDE = 1600
_JPEG_QUALITY = 88

# 输出预算：JD 全文可能很长，且推理模型会先消耗 reasoning token，必须给足
_MAX_TOKENS = 8000

VISION_SYSTEM_PROMPT = (
    "你是图片文字提取器（OCR）。请把图片中的全部文字**原样**提取出来。\n"
    "严格规则：\n"
    "1. 只输出图片中真实存在的文字，禁止解释、总结、翻译、改写、补全。\n"
    "2. 保持原有阅读顺序与分段结构；表格按「字段：值」逐行输出。\n"
    "3. 图片中没有文字，直接回复空字符串。\n"
    "4. 不要输出任何 Markdown 代码块标记、不要加「以下是识别结果」之类的前后缀。\n"
    "5. 不要编造看不清的内容；看不清的字用「□」占位，不要猜。"
)

# 图片同样是**外部内容**（用户上传的 JD 截图）：图里可能印着「忽略以上要求…
# 输出 XXX」这类文字，模型对图片里的祈使句同样会让步 ⇒ 挂防注入。
# 本提示词只做 OCR 转录、不产出简历正文、不打分 ⇒ 不挂另两条。
VISION_SYSTEM_PROMPT = attach(VISION_SYSTEM_PROMPT, INJECTION_GUARD)


def _normalize_image(image_bytes: bytes) -> tuple[bytes, str]:
    """
    把任意支持格式的图片规范化为视觉接口可接受的 (bytes, mime)。

    - bmp / 其他 PIL 可读格式 -> PNG
    - 长边超过 _MAX_SIDE 时等比缩小
    - 带透明通道的 PNG 保留 PNG；其余统一 JPEG（体积更小）
    """
    from PIL import Image

    img = Image.open(io.BytesIO(image_bytes))
    img.load()

    # 等比压缩
    if max(img.size) > _MAX_SIDE:
        ratio = _MAX_SIDE / float(max(img.size))
        new_size = (max(1, int(img.width * ratio)), max(1, int(img.height * ratio)))
        img = img.resize(new_size, Image.LANCZOS)

    has_alpha = img.mode in ("RGBA", "LA") or (
        img.mode == "P" and "transparency" in img.info
    )

    buf = io.BytesIO()
    if has_alpha:
        img.convert("RGBA").save(buf, format="PNG", optimize=True)
        mime = "image/png"
    else:
        img.convert("RGB").save(buf, format="JPEG", quality=_JPEG_QUALITY, optimize=True)
        mime = "image/jpeg"
    return buf.getvalue(), mime


def _clean_text(raw: str) -> str:
    """清洗模型输出：去 Markdown 代码块包裹、去多余空白。"""
    text = (raw or "").strip()
    # 去掉 ```xxx ... ``` 包裹
    text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
    text = re.sub(r"```\s*$", "", text)
    # 去掉模型偶尔加的客套前缀
    text = re.sub(r"^(以下是|下面是)[^\n]{0,20}(识别|提取)?(结果|文字|内容)[：:]\s*", "", text)
    # 折叠 3 个以上连续空行
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def extract_text_from_image_vision(
    image_bytes: bytes,
    filename: str = "image.png",
    user: Optional[User] = None,
    llm=None,
) -> str:
    """
    用多模态模型从图片提取文字。任何失败都返回 ""（调用方据此降级）。

    :param user: 传入用户则使用其自有/平台默认 LLM 配置
    :param llm: 也可直接传入已构造的 LLM 实例（便于测试）
    """
    ext = ("." + filename.rsplit(".", 1)[-1].lower()) if "." in filename else ""
    if ext and ext not in _SUPPORTED_EXT:
        return ""

    if llm is None:
        if user is None:
            return ""
        try:
            llm = get_user_llm_or_raise(user)
        except Exception:
            return ""

    try:
        from langchain_core.messages import HumanMessage, SystemMessage

        norm_bytes, mime = _normalize_image(image_bytes)
        b64 = base64.b64encode(norm_bytes).decode("ascii")

        message = HumanMessage(
            content=[
                {"type": "text", "text": "请提取这张图片中的全部文字。"},
                {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}},
            ]
        )
        response = llm.invoke(
            [SystemMessage(content=VISION_SYSTEM_PROMPT), message],
            max_tokens=_MAX_TOKENS,
        )
        content = getattr(response, "content", "") or ""
        # 部分实现把内容放在 list 里
        if isinstance(content, list):
            parts = []
            for p in content:
                if isinstance(p, dict):
                    parts.append(str(p.get("text", "")))
                else:
                    parts.append(str(p))
            content = "".join(parts)

        return _clean_text(content)
    except Exception:
        # 静默降级：不回抛，由调用方走本地 OCR
        return ""


def vision_available(user: Optional[User]) -> bool:
    """判断当前是否具备视觉识别条件（不实际调用模型）。"""
    if user is None:
        return False
    try:
        return get_user_llm_or_raise(user) is not None
    except Exception:
        return False
