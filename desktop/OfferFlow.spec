# -*- mode: python ; coding: utf-8 -*-

import os

from PyInstaller.utils.hooks import collect_all  # noqa: E402

# ---- 本地文档解析（liteparse，P1）----
# liteparse 带两个原生文件：_liteparse.pyd（20MB，Rust 扩展）与 pdfium.dll（7MB，PDFium 引擎）。
# 二者都不在静态分析的收集范围内——**只写 hiddenimports 一定会漏**，打包后一解析 PDF 就
# 报 ImportError / 找不到 pdfium.dll。故用 collect_all 一次性收齐子模块 + 数据 + 动态库。
_liteparse_bins, _liteparse_datas, _liteparse_hidden = collect_all('liteparse')

# ---- 本地 embedding（fastembed + onnxruntime，P2）----
# 两个坑：
#   1. onnxruntime 是原生扩展（onnxruntime_pybind11_state.pyd + 若干 dll），
#      不在静态分析范围 → 不显式收就会打包后 `import fastembed` 直接失败；
#   2. bge 模型是**运行时按磁盘路径读取**的（embedder.model_dir() → specific_model_path），
#      完全不经过 import 机制 → 必须作为 datas 随包附带，否则打包后模型缺失、
#      只能回退联网下载（破坏离线优先）。故下面把整个模型目录塞进 datas。
_FASTEMBED_BINS, _FASTEMBED_DATAS, _FASTEMBED_HIDDEN = collect_all('fastembed')
_ONNX_BINS, _ONNX_DATAS, _ONNX_HIDDEN = collect_all('onnxruntime')

# 随包分发的 embedding 模型（91MB）。目录缺失时给出显式告警，避免打出一个
# "看起来正常、一跑检索就联网下载" 的包。
_MODEL_REL = os.path.join('models', 'fast-bge-small-zh-v1.5')
_MODEL_SRC = os.path.join(SPECPATH, _MODEL_REL)
_MODEL_DATAS = []
if os.path.isdir(_MODEL_SRC):
    _MODEL_DATAS.append((_MODEL_SRC, _MODEL_REL))
else:
    print('WARNING: 未找到本地 embedding 模型 %s —— 打包后需联网下载模型！' % _MODEL_SRC)

# ---- 首启种子索引（P3 收尾，2026-09-30）----
# 只含共享语料（corpus）的「算好的索引 + 快照」，让用户**开箱即有离线检索**：
# 否则首启要先联网跑一次约 20s 的全量重建，断网首启等于完全没有检索能力。
# 由 tools/build_seed.py 生成。缺失时显式告警——功能不受影响，只是首启需联网。
_SEED_REL = os.path.join('seed', 'local_ai')
_SEED_SRC = os.path.join(SPECPATH, _SEED_REL)
_SEED_DATAS = []
if os.path.isdir(_SEED_SRC) and os.path.exists(os.path.join(_SEED_SRC, 'index.db')):
    # 白名单收集，**不要**整目录收集：SQLite 在 WAL 模式下会在种子目录留下
    # index.db-wal / index.db-shm，整目录收集会把它们一起打进安装包（无用且过时），
    # 而铺设端（local_ai/seed.py）只复制 index.db —— 二者不一致时种子可能静默残缺。
    _SEED_DATAS.append((os.path.join(_SEED_SRC, 'index.db'), _SEED_REL))
    _seed_cache = os.path.join(_SEED_SRC, 'cache')
    if os.path.isfile(os.path.join(_seed_cache, 'corpus.json')):
        _SEED_DATAS.append(
            (os.path.join(_seed_cache, 'corpus.json'), os.path.join(_SEED_REL, 'cache')))
else:
    print('WARNING: 未找到种子索引 %s —— 首启需联网才能建立本地检索'
          '（跑 tools/build_seed.py 生成）' % _SEED_SRC)

# ---- 产物瘦身 ----
# QtWebEngine 会带进来约 450MB 资源，其中两类对本应用（WebView 外壳）无用：
#   1. 调试版资源：*.debug.pak（约 80MB）、v8_context_snapshot.debug.bin
#      —— 只在开发者调试 DevTools 时加载，正常启动/运行走的是非 debug 版本；
#   2. 多余语言包：Qt translations 158 个 .qm + qtwebengine_locales 各国 pak
#      —— 界面只用到中文（英文作兜底），其余全部剔除。
# 注意：剔除后必须实跑一次 EXE 验证能正常加载页面（见发版检查清单）。
_KEEP_LOCALES = {"zh-CN.pak", "en-US.pak", "en-GB.pak"}


def _keep_data(entry):
    # 注意：a.datas 里的目标名是「相对路径」，如 PySide6/translations/qt_zh_CN.qm，
    # 没有前导斜杠。这里统一补一个再判断，否则 "/PySide6/..." 永远匹配不上。
    raw = entry[0].replace("\\", "/")
    name = raw if raw.startswith("/") else "/" + raw
    base = os.path.basename(raw)
    if base.endswith(".debug.pak") or base.endswith(".debug.bin"):
        return False
    if "/PySide6/translations/" in name:
        if "/qtwebengine_locales/" in name:
            return base in _KEEP_LOCALES
        return base.endswith("_zh_CN.qm") or base.endswith("_en.qm")
    return True


a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=_liteparse_bins + _FASTEMBED_BINS + _ONNX_BINS,
    datas=_liteparse_datas + _FASTEMBED_DATAS + _ONNX_DATAS + _MODEL_DATAS + _SEED_DATAS,
    # QtPdf / QtPdfWidgets：版式预览需要用真实 PDF 渲染（QPdfDocument），
    # 不在默认收集范围里，漏了会「选版式时白屏」。字体在运行时从系统字体目录读，
    # 不需要打进包（见 core/font_subset.py）。
    hiddenimports=[
        'PySide6.QtPdf',
        'PySide6.QtPdfWidgets',
        'pypdf',
        # 云端参考库：services/cloud.py 是函数内懒加载（from . import cloud），
        # 显式声明避免打包时漏掉导致「生成/优化简历」静默拿不到云端参考。
        'offerflow_desktop.services.cloud',
        # 更新检查：services/updater.py 在线程里懒加载，同样显式声明。
        'offerflow_desktop.services.updater',
        # 云端 API 客户端（原生版数据/AI 走云端）
        'offerflow_desktop.api_client',
        # Windows 系统 OCR（core/ocr.py「截图识别 JD」）：
        # 调用写在函数体内，靠静态分析收集不稳，显式声明避免 OCR 静默失效
        # （表现为点按钮提示「本机未启用 Windows OCR 引擎」）。
        # 对应 requirements.txt 里的 winrt-* 8 个包，缺一个都会在运行时 ImportError。
        'winrt',
        'winrt.windows.globalization',
        'winrt.windows.media.ocr',
        'winrt.windows.graphics.imaging',
        'winrt.windows.storage',
        'winrt.windows.storage.streams',
        'winrt.windows.foundation',
        'winrt.windows.foundation.collections',
        # 本地文档解析（local_ai/parse.py）：liteparse 子模块 + 原生库，
        # 由文件顶部 collect_all('liteparse') 收集（_liteparse.pyd / pdfium.dll 在内）。
        *_liteparse_hidden,
        # 本地 embedding（local_ai/embedder.py）：fastembed 在函数体内懒加载，
        # onnxruntime 是原生扩展，二者都由文件顶部 collect_all 收集。
        *_FASTEMBED_HIDDEN,
        *_ONNX_HIDDEN,
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)

# Codex/Poppler may be earlier on PATH and contributes an ICU build whose
# exports are suffixed (e.g. ucnv_open_78); Qt expects the Windows ICU ABI.
# Force the system ICU pair so a clean rebuild always produces a loadable Qt runtime.
_SYSTEM32 = os.path.join(os.environ.get("SystemRoot", r"C:\\Windows"), "System32")
_ICU_NAMES = {"icuuc.dll", "icu.dll"}
_SYSTEM_ICU = []
for _name in sorted(_ICU_NAMES):
    _src = os.path.join(_SYSTEM32, _name)
    if not os.path.isfile(_src):
        raise RuntimeError("Missing Windows system ICU file: %s" % _src)
    _SYSTEM_ICU.append((_name, _src, "BINARY"))
a.binaries = [
    entry for entry in a.binaries
    if os.path.basename(entry[0]).lower() not in _ICU_NAMES
] + _SYSTEM_ICU

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='OfferFlow',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=['icon.ico'],
)
coll = COLLECT(
    exe,
    a.binaries,
    # 应用瘦身过滤（见文件开头说明）
    [d for d in a.datas if _keep_data(d)],
    strip=False,
    upx=True,
    upx_exclude=[],
    name='OfferFlow',
)
