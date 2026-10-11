"""桌面版更新：检查云端 version.json → 提示新版 → 一键静默覆盖升级。

「一键更新」的链路：下载新版安装包到临时目录 → 用 /SILENT /DIR=<当前安装目录>
拉起安装程序（覆盖安装，不重新选目录、不弹「文件夹已存在」）→ 本进程退出 →
安装程序装完自动拉起新版。
"""
from __future__ import annotations

import hashlib
import os
import subprocess
import sys
from typing import Callable, Dict, Optional

import requests

from ..config import APP_VERSION


def _parse_parts(v: str):
    out = []
    for p in str(v or "").replace("v", "").replace("V", "").split("."):
        try:
            out.append(int(p))
        except ValueError:
            out.append(0)
    while len(out) < 3:
        out.append(0)
    return out[:3]


def is_newer(latest: str, current: str) -> bool:
    """按数字段比较语义版本号。"""
    return _parse_parts(latest) > _parse_parts(current)


def check_for_update(timeout: float = 6.0) -> Optional[Dict]:
    """拉取云端版本信息，返回可展示的更新字典；无更新/失败返回 None。

    返回：{current, latest, notes, download_url}
    """
    try:
        from . import cloud
        base = cloud.get_cloud_url()
        url = base + "/downloads/version.json"
        resp = requests.get(url, timeout=timeout)
        resp.raise_for_status()
        info = resp.json()
        if not isinstance(info, dict):
            return None
        latest = str(info.get("version") or "").strip()
        if not latest or not is_newer(latest, APP_VERSION):
            return None
        dl = str(info.get("download_url") or "").strip()
        if dl.startswith("/"):
            dl = base + dl
        if not dl:
            dl = base + "/downloads/OfferFlow-Setup.exe"
        return {
            "current": APP_VERSION,
            "latest": latest,
            "notes": (info.get("notes") or "").strip(),
            "download_url": dl,
            # version.json 未提供 sha256 时留空：校验函数会跳过（向后兼容老版
            # version.json），但新版发布必须带上，否则无法抵御下载被篡改。
            "sha256": (info.get("sha256") or "").strip(),
        }
    except Exception:  # noqa: BLE001
        return None


def install_dir() -> str:
    """当前安装目录（打包运行 = exe 所在目录；源码运行 = 项目根）。"""
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    here = os.path.abspath(__file__)                       # <root>/offerflow_desktop/services/updater.py
    return os.path.dirname(os.path.dirname(os.path.dirname(here)))


def setup_cache_path(version: str, download_dir: Optional[str] = None) -> str:
    """新版安装包的落地路径。

    download_dir 由调用方传入（通常来自 database.resolve_download_dir，
    尊重用户在「系统设置」里选的下载目录）；为空时用系统临时目录兜底。
    按版本号命名，避免不同版本互相覆盖，也便于「已下载则跳过」复用。
    """
    base = (download_dir or "").strip() or os.environ.get("TEMP") or os.path.expanduser("~")
    return os.path.join(base, "OfferFlow-Update", f"OfferFlow-Setup-{version}.exe")


def verify_file_sha256(path: str, expected: str) -> bool:
    """计算文件 SHA256 并与期望值（大小写不敏感）比较。

    任何读文件/哈希异常都返回 False，调用方据此判定校验失败。
    """
    try:
        exp = str(expected or "").strip().lower()
        if not exp:
            return False
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                h.update(chunk)
        return h.hexdigest() == exp
    except Exception:  # noqa: BLE001
        return False


def download_installer(url: str, dest: str,
                       progress: Optional[Callable[[int, int], None]] = None,
                       timeout: tuple = (10, 300),
                       expected_sha256: Optional[str] = None) -> bool:
    """流式下载新版安装包；progress(已下载字节, 总字节) 用于界面进度回调。

    先写 .part 再原子改名，避免下载中断留下半截文件被当成可用安装包。
    expected_sha256 非空时，下载完成后校验完整性：不匹配则删除文件并抛异常，
    绝不让被损坏/被篡改的安装包进入静默升级环节。
    """
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    # 零副作用优化：若目标版本的安装包已下载且 sha256 校验通过，直接复用，
    # 不再重复下载（取消后重试 / 重复点「立即更新」都不会再拉一遍 119MB）。
    # 注意：expected 为空（老 version.json 无 sha256）时 verify_file_sha256 返回
    # False，不会误判「存在即可用」而跳过完整性校验。
    exp = (expected_sha256 or "").strip()
    if os.path.exists(dest) and verify_file_sha256(dest, exp):
        if progress is not None:
            try:
                _sz = os.path.getsize(dest)
                progress(_sz, _sz)
            except Exception:  # noqa: BLE001
                pass
        return True
    tmp = dest + ".part"
    with requests.get(url, stream=True, timeout=timeout) as resp:
        resp.raise_for_status()
        total = int(resp.headers.get("Content-Length") or 0)
        done = 0
        with open(tmp, "wb") as f:
            for chunk in resp.iter_content(chunk_size=256 * 1024):
                if not chunk:
                    continue
                f.write(chunk)
                done += len(chunk)
                if progress is not None:
                    try:
                        progress(done, total)
                    except Exception:  # noqa: BLE001
                        pass
    os.replace(tmp, dest)
    # 审计复核（P1-4 强化）：sha256 缺失或哈希不匹配都视为校验失败，一律拒绝升级。
    # 不能「空值跳过」——否则中间人只需删除 version.json 里的 sha256 字段即可
    # 绕过完整性校验、投递被篡改的安装包。verify_file_sha256 对空期望返回 False。
    if not verify_file_sha256(dest, expected_sha256):
        try:
            os.remove(dest)
        except OSError:
            pass
        raise ValueError(
            "安装包完整性校验失败（sha256 缺失或不匹配）：已拒绝升级，以防下载损坏"
            "或被篡改的安装包。请确认 version.json 正确携带 sha256 字段。"
        )
    return True


def run_silent_upgrade(setup_path: str) -> bool:
    """静默覆盖升级到「当前安装目录」，装完由安装程序自动拉起新版。

    /DIR 显式指定当前目录：不依赖注册表里有没有卸载记录，也就不会再出现
    「重新选目录 / 提示文件夹已存在」。
    """
    args = [
        setup_path,
        "/SILENT",
        "/SUPPRESSMSGBOXES",
        "/NORESTART",
        "/CLOSEAPPLICATIONS",
        "/FORCECLOSEAPPLICATIONS",
        "/DIR=" + install_dir(),
    ]
    try:
        subprocess.Popen(args, close_fds=True)
        return True
    except Exception:  # noqa: BLE001
        return False


# ============================================================================
# 增量更新（内容寻址 manifest）
# ----------------------------------------------------------------------------
# 为什么需要：交付物原本是 Inno Setup 单体全量安装器（约 119MB），版本一变就
# 整个重下。而实测两版之间真正变化的文件只占约 4%（2.2.19→2.2.20：703 个文件
# 里只有 2 个变，共 14MB / 361MB）。故改为「按文件内容寻址」的增量更新：
#
#   manifest.json 列出安装目录下每个文件的 (相对路径, 字节数, sha256)；
#   内容块按 sha256 存放于 files/<sha[:2]>/<sha>，跨版本共享同一份。
#
# 客户端拿 manifest 逐个比对本地哈希，只下载「缺失或不同」的文件，下载后算出
# 的 sha256 必须等于 URL 里的 sha（地址即校验，天然抗篡改），再由一个 bat
# 引导脚本在主进程退出后完成替换与重启。
#
# 注意：运行中的 exe / 已被加载的 dll 在 Windows 上不允许直接覆盖，所以替换
# 必须发生在主进程退出之后 —— 这正是 bat 引导脚本存在的唯一理由。
# ============================================================================

MANIFEST_PATH = "/downloads/pkg/manifest.json"
_PLAN_NAME = "_plan.txt"
_REMOVE_NAME = "_remove.txt"
_APPLY_NAME = "_apply_update.bat"


def _cloud_base() -> str:
    """云端根地址（不抛异常；取不到时返回空串，由调用方判定不可用）。"""
    try:
        from . import cloud
        return str(cloud.get_cloud_url() or "").rstrip("/")
    except Exception:  # noqa: BLE001
        return ""


def fetch_manifest(timeout: float = 10.0) -> Optional[Dict]:
    """拉取增量更新清单；不可用/失败返回 None（调用方据此回退全量安装器）。"""
    try:
        base = _cloud_base()
        if not base:
            return None
        resp = requests.get(base + MANIFEST_PATH, timeout=timeout)
        resp.raise_for_status()
        m = resp.json()
        if not isinstance(m, dict):
            return None
        files = m.get("files")
        if not isinstance(files, dict) or not files or not str(m.get("version") or "").strip():
            return None
        return m
    except Exception:  # noqa: BLE001
        return None


def incremental_supported() -> bool:
    """只有打包运行（frozen）才做增量；源码运行没有「安装目录」可就地替换。"""
    return bool(getattr(sys, "frozen", False))


def plan_incremental(manifest: Dict, root: str) -> Dict:
    """比对本地安装目录与 manifest，得出需要下载/删除的文件。

    返回 {download: [{rel,size,sha}], remove: [rel], total_bytes, total_files, unchanged}
    """
    files = manifest.get("files") or {}
    to_dl: list = []
    for rel, meta in files.items():
        if not isinstance(meta, dict):
            continue
        h = str(meta.get("h") or "").strip().lower()
        size = int(meta.get("s") or 0)
        if not h:
            continue
        local = os.path.join(root, str(rel).replace("/", os.sep))
        if os.path.isfile(local) and os.path.getsize(local) == size and verify_file_sha256(local, h):
            continue  # 内容一致 → 不下载（这正是省下 119MB 的地方）
        to_dl.append({"rel": str(rel), "size": size, "sha": h})

    to_rm: list = []
    for rel in manifest.get("removed") or []:
        local = os.path.join(root, str(rel).replace("/", os.sep))
        if os.path.isfile(local):
            to_rm.append(str(rel))

    return {
        "download": to_dl,
        "remove": to_rm,
        "total_bytes": sum(x["size"] for x in to_dl),
        "total_files": len(to_dl),
        "unchanged": max(0, len(files) - len(to_dl)),
    }


def block_url(manifest: Dict, sha: str) -> str:
    """内容块的下载地址（地址里带着 sha256，下载后必须算得同一个值）。"""
    base = str(manifest.get("base") or "").strip().rstrip("/")
    if not base:
        base = _cloud_base().rstrip("/") + "/downloads/pkg"
    return f"{base}/files/{sha[:2]}/{sha}"


def stage_updates(manifest: Dict, root: str, staging: str,
                  progress: Optional[Callable[[int, int, int, int], None]] = None,
                  timeout: tuple = (10, 300)) -> Dict:
    """把「需要下载的文件」拉到暂存目录，逐个校验 sha256。

    progress(已下载字节, 总字节, 已完成文件数, 总文件数)。
    返回 {ok: bool, staged: [rel], error: str}。
    """
    plan = plan_incremental(manifest, root)
    items = plan["download"]
    total_bytes = plan["total_bytes"] or 1
    done_bytes = 0
    staged: list = []

    plan_lines: list = []
    for idx, item in enumerate(items):
        rel, sha = item["rel"], item["sha"]
        dest = os.path.join(staging, rel.replace("/", os.sep))
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        # 断点续传：暂存区里已有且校验通过的文件直接复用
        if not (os.path.isfile(dest) and verify_file_sha256(dest, sha)):
            tmp = dest + ".part"
            url = block_url(manifest, sha)
            with requests.get(url, stream=True, timeout=timeout) as resp:
                resp.raise_for_status()
                with open(tmp, "wb") as f:
                    for chunk in resp.iter_content(chunk_size=256 * 1024):
                        if not chunk:
                            continue
                        f.write(chunk)
                        done_bytes += len(chunk)
                        if progress is not None:
                            try:
                                progress(done_bytes, total_bytes, idx, len(items))
                            except Exception:  # noqa: BLE001
                                pass
            os.replace(tmp, dest)
            if not verify_file_sha256(dest, sha):
                try:
                    os.remove(dest)
                except OSError:
                    pass
                return {"ok": False, "staged": staged,
                        "error": f"内容块校验失败：{rel}（sha256 与地址不一致，已拒绝）"}
        else:
            done_bytes += item["size"]
        staged.append(rel)
        plan_lines.append(f"{rel}|{dest}")
        if progress is not None:
            try:
                progress(done_bytes, total_bytes, idx + 1, len(items))
            except Exception:  # noqa: BLE001
                pass

    with open(os.path.join(staging, _PLAN_NAME), "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(plan_lines))
    with open(os.path.join(staging, _REMOVE_NAME), "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(plan["remove"]))
    return {"ok": True, "staged": staged, "error": "", "plan": plan}


def write_apply_script(root: str, staging: str, manifest: Dict, pid: int) -> str:
    """生成在主进程退出后完成替换 + 重启的 bat 引导脚本。

    必须先退出再替换：Windows 不允许覆盖正在运行/已被加载的 exe 与 dll。
    脚本全程 ASCII（避免中文在 GBK 控制台下乱码），路径一律加引号。
    """
    exe = str(manifest.get("exe") or "OfferFlow.exe").replace("/", os.sep)
    log = os.path.join(staging, "_apply.log")
    plan_txt = os.path.join(staging, _PLAN_NAME)
    remove_txt = os.path.join(staging, _REMOVE_NAME)

    lines = [
        "@echo off",
        "setlocal",
        f'set "ROOT={root}"',
        f'set "STAGE={staging}"',
        f'set "EXE={exe}"',
        f'set "PID={pid}"',
        f'set "LOG={log}"',
        'echo [%date% %time%] apply start >> "%LOG%"',
        "set /a N=0",
        ":WAIT",
        "set /a N+=1",
        "if %N% GTR 60 goto APPLY",
        f'tasklist /FI "PID eq %PID%" /NH 2>nul | findstr /I "%PID%" >nul',
        "if not errorlevel 1 (ping -n 2 127.0.0.1 >nul & goto WAIT)",
        ":APPLY",
        "ping -n 2 127.0.0.1 >nul",
        "set FAIL=0",
        "if exist \"%STAGE%\\_remove.txt\" (",
        "  for /f \"usebackq delims=\" %%R in (\"%STAGE%\\_remove.txt\") do (",
        '    del /F /Q "%ROOT%\\%%~R" >> "%LOG%" 2>nul',
        "  )",
        ")",
        "for /f \"usebackq tokens=1,2 delims=|\" %%A in (\"%STAGE%\\_plan.txt\") do (",
        '  if exist "%ROOT%\\%%~A" move /Y "%ROOT%\\%%~A" "%ROOT%\\%%~A.ofbak" >> "%LOG%" 2>nul',
        '  move /Y "%%~B" "%ROOT%\\%%~A" >> "%LOG%" 2>nul',
        "  if errorlevel 1 (",
        '    echo FAIL %%~A >> "%LOG%"',
        "    set FAIL=1",
        '    if exist "%ROOT%\\%%~A.ofbak" move /Y "%ROOT%\\%%~A.ofbak" "%ROOT%\\%%~A" >> "%LOG%" 2>nul',
        "  )",
        ")",
        'if "%FAIL%"=="1" (',
        '  echo ROLLBACK >> "%LOG%"',
        ') else (',
        '  echo OK >> "%LOG%"',
        ")",
        'start "" "%ROOT%\\%EXE%"',
        "endlocal",
    ]
    path = os.path.join(staging, _APPLY_NAME)
    # 显式 LF→CRLF：bat 在 Windows 下用 LF 换行有概率解析异常，别赌。
    with open(path, "w", encoding="utf-8", newline="\r\n") as f:
        f.write("\n".join(lines) + "\n")
    return path


def launch_apply_and_exit(script_path: str) -> bool:
    """以脱离父进程的方式拉起引导脚本（父进程随后退出，替换才会成功）。

    ⚠ 窗口策略（2026-10-10 修：此处曾用 DETACHED_PROCESS，会让用户看到一个黑窗）：
    ``DETACHED_PROCESS`` 的语义是「子进程**没有**控制台」，可 ``cmd.exe`` 本身是
    控制台程序 —— 它一旦发现自己没有控制台，就会**自己新建一个可见的**。Win11 下
    又由 Windows Terminal 接管，于是用户点「自动更新 → 立即安装」时眼前弹出一个
    标题为 ``findstr /I "<PID>"`` 的黑窗（bat 里轮询 ``tasklist | findstr`` 那一行，
    因 ``@echo off`` + ``>nul`` 而全黑、无任何信息），看起来像报错。

    改用 ``CREATE_NO_WINDOW``：给子进程一个**隐藏的**控制台。bat 里的
    ``tasklist`` / ``findstr`` / ``ping`` 照常工作，窗口不可见。
    两者都让子进程独立于父进程，父进程 os._exit 后仍继续跑完替换 + 重启。

    注意 ``CREATE_NO_WINDOW`` 与 ``DETACHED_PROCESS`` 互斥，不可叠加。
    """
    try:
        flags = 0
        if os.name == "nt":
            flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(
                subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        subprocess.Popen(
            ["cmd.exe", "/c", script_path],
            close_fds=True, creationflags=flags,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        return True
    except Exception:  # noqa: BLE001
        return False
