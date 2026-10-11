"""文档解析三件套（MinerU / Docling / MarkItDown）接入 OfferFlow，优雅降级。

解析优先级：MinerU (中文/复杂表格/公式最强) > Docling (IBM 结构化, MIT 可离线)
> MarkItDown (微软 Office 轻量) > 基础文本提取 (pypdf)。

实现方式：全部走 CLI（subprocess），库未安装时该层自动跳过，不报错、不阻断。
部署启用：服务端 `pip install mineru docling markitdown` 后，设
DOC_PARSE_ENABLED=true 即自动按优先级生效；未启用时返回基础提取结果，
保证主流程（简历/JD/语料入库）永远不崩。资源受限说明：MinerU 较重（建议 GPU/
大内存机），Docling 次之，MarkItDown 最轻——按服务器内存选择安装即可。
"""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import List, Optional

log = logging.getLogger("offerflow.doc_parse")

DOC_PARSE_ENABLED = os.getenv("DOC_PARSE_ENABLED", "false").lower() == "true"

# 各解析器候选 CLI 名（按优先级）
_MINERU_CMDS = ["mineru", "magic-pdf"]
_DOCLING_CMD = "docling"
_MARKITDOWN_CMD = "markitdown"


def _which_any(cmds: List[str]) -> Optional[str]:
    for c in cmds:
        p = shutil.which(c)
        if p:
            return p
    return None


def _read_first_md(out_dir: str) -> Optional[str]:
    d = Path(out_dir)
    if not d.exists():
        return None
    mds = sorted(d.rglob("*.md"))
    if mds:
        try:
            return mds[0].read_text(encoding="utf-8", errors="ignore")
        except Exception:
            return None
    for ext in ("*.txt", "*.json"):
        fs = sorted(d.rglob(ext))
        if fs:
            try:
                return fs[0].read_text(encoding="utf-8", errors="ignore")
            except Exception:
                return None
    return None


def _run(cmd: List[str], timeout: int = 300) -> Optional[str]:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        if r.returncode != 0:
            log.warning("[doc_parse] 命令失败 %s | stderr=%s", cmd, r.stderr[:200])
            return None
        return r.stdout
    except Exception as e:
        log.warning("[doc_parse] 命令异常 %s | %s", cmd, e)
        return None


def _parse_mineru(path: Path) -> Optional[str]:
    exe = _which_any(_MINERU_CMDS)
    if not exe:
        return None
    out = tempfile.mkdtemp(prefix="mineru_")
    try:
        _run([exe, "-p", str(path), "-o", out], timeout=600)
        return _read_first_md(out)
    except Exception as e:
        log.warning("[doc_parse] MinerU 异常: %s", e)
        return None


def _parse_docling(path: Path) -> Optional[str]:
    exe = _which_any([_DOCLING_CMD])
    if not exe:
        return None
    out = tempfile.mkdtemp(prefix="docling_")
    try:
        _run([exe, str(path), "-o", out], timeout=600)
        return _read_first_md(out)
    except Exception as e:
        log.warning("[doc_parse] Docling 异常: %s", e)
        return None


def _parse_markitdown(path: Path) -> Optional[str]:
    exe = _which_any([_MARKITDOWN_CMD])
    if not exe:
        return None
    try:
        return _run([exe, str(path)], timeout=300)
    except Exception as e:
        log.warning("[doc_parse] MarkItDown 异常: %s", e)
        return None


def _parse_basic(path: Path) -> Optional[str]:
    """基础兜底：pypdf 提取纯文本。"""
    try:
        from pypdf import PdfReader
    except Exception:
        try:
            from PyPDF2 import PdfReader
        except Exception:
            return None
    try:
        reader = PdfReader(str(path))
        return "\n".join((p.extract_text() or "") for p in reader.pages)
    except Exception as e:
        log.warning("[doc_parse] 基础提取失败: %s", e)
        return None


def parse_to_markdown(file_bytes: bytes, filename: str) -> str:
    """统一解析入口：返回 markdown 文本。始终成功（最坏返回空串）。"""
    suffix = Path(filename).suffix or ".bin"
    tmp = Path(tempfile.mktemp(suffix=suffix, prefix="docparse_"))
    tmp.write_bytes(file_bytes)
    try:
        if DOC_PARSE_ENABLED:
            for fn in (_parse_mineru, _parse_docling, _parse_markitdown):
                try:
                    md = fn(tmp)
                except Exception as e:
                    log.warning("[doc_parse] %s 异常: %s", fn.__name__, e)
                    md = None
                if md and md.strip():
                    log.info("[doc_parse] 使用 %s 解析成功", fn.__name__)
                    return md
        basic = _parse_basic(tmp)
        if basic and basic.strip():
            return basic
        return ""
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass


def parser_status() -> dict:
    """当前可用的解析器清单（供 /health 或管理后台展示）。"""
    available = {
        "mineru": bool(_which_any(_MINERU_CMDS)),
        "docling": bool(_which_any([_DOCLING_CMD])),
        "markitdown": bool(_which_any([_MARKITDOWN_CMD])),
    }
    return {"enabled": DOC_PARSE_ENABLED, "available": available}
