"""Image validation and size reduction shared by upload flows."""

import io

from PIL import Image, ImageOps, UnidentifiedImageError


_MIME_BY_FORMAT = {
    "JPEG": ("image/jpeg", ".jpg"),
    "PNG": ("image/png", ".png"),
    "GIF": ("image/gif", ".gif"),
    "WEBP": ("image/webp", ".webp"),
    "BMP": ("image/bmp", ".bmp"),
}


def _jpeg_bytes(image: Image.Image, quality: int) -> bytes:
    if image.mode in ("RGBA", "LA") or (
        image.mode == "P" and "transparency" in image.info
    ):
        rgba = image.convert("RGBA")
        background = Image.new("RGB", rgba.size, "white")
        background.paste(rgba, mask=rgba.getchannel("A"))
        image = background
    elif image.mode != "RGB":
        image = image.convert("RGB")

    output = io.BytesIO()
    image.save(
        output,
        format="JPEG",
        quality=quality,
        optimize=True,
        progressive=True,
    )
    return output.getvalue()


def _gif_bytes(image: Image.Image, max_dimension: int) -> bytes | None:
    if not getattr(image, "is_animated", False):
        return None
    frames = []
    durations = []
    try:
        for frame_index in range(getattr(image, "n_frames", 1)):
            image.seek(frame_index)
            frame = image.convert("RGBA")
            frame.thumbnail((max_dimension, max_dimension), Image.Resampling.LANCZOS)
            frames.append(frame.convert("P", palette=Image.Palette.ADAPTIVE))
            durations.append(image.info.get("duration", 100))
        output = io.BytesIO()
        frames[0].save(
            output,
            format="GIF",
            save_all=True,
            append_images=frames[1:],
            duration=durations,
            loop=image.info.get("loop", 0),
            optimize=True,
            disposal=2,
        )
        return output.getvalue()
    except Exception:
        return None


def compress_image(
    data: bytes,
    *,
    max_bytes: int,
    max_dimension: int = 2048,
) -> tuple[bytes, str, str]:
    """Return ``(bytes, mime, extension)`` while ensuring the result fits.

    Small images are returned byte-for-byte after validation. Oversized static
    images are resized and re-encoded as JPEG; oversized GIFs are first resized
    with animation retained and fall back to a JPEG cover if still too large.
    """
    if not data:
        raise ValueError("图片内容为空")

    try:
        with Image.open(io.BytesIO(data)) as source:
            source.load()
            source_format = (source.format or "").upper()
            mime, extension = _MIME_BY_FORMAT.get(
                source_format, ("application/octet-stream", ".img")
            )
            if len(data) <= max_bytes:
                return data, mime, extension

            if source.width * source.height > 40_000_000:
                raise ValueError("图片分辨率过大，请先缩小后再上传")

            if source_format == "GIF" and getattr(source, "is_animated", False):
                for dimension in (max_dimension, 1280, 960, 640, 480):
                    reduced = _gif_bytes(source, dimension)
                    if reduced and len(reduced) <= max_bytes:
                        return reduced, "image/gif", ".gif"
                source.seek(0)

            image = ImageOps.exif_transpose(source)
            dimension = min(max_dimension, max(image.size))
            while dimension >= 320:
                reduced = image.copy()
                reduced.thumbnail((dimension, dimension), Image.Resampling.LANCZOS)
                for quality in (88, 80, 72, 64):
                    encoded = _jpeg_bytes(reduced, quality)
                    if len(encoded) <= max_bytes:
                        return encoded, "image/jpeg", ".jpg"
                dimension = int(dimension * 0.8)
    except ValueError:
        raise
    except (UnidentifiedImageError, OSError, SyntaxError) as exc:
        raise ValueError("无法识别或处理该图片") from exc

    raise ValueError("图片压缩后仍超过大小限制，请选择尺寸更小的图片")
