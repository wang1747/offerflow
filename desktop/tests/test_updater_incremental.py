"""增量更新：只下载变化文件的完整链路测试（含反向对照）。

判据必须是「通过与否只取决于被测代码」，所以这里用**本机 HTTP 服务**提供内容块，
不碰外网、不碰云端真实 manifest。

覆盖：
  1. block_url 内容寻址格式（地址里带着 sha256）
  2. plan_incremental：本地一致→不下载 / 不同→下载 / 缺失→下载 / removed→删除
  3. stage_updates：真下载 + 逐块 sha256 校验 + plan/remove 清单落盘 + 断点续传
  4. 反向对照：服务器返回「内容与地址 sha 不符」的块 → 必须拒绝（证明校验不是摆设）
  5. write_apply_script：bat 内容可替换可执行、CRLF、纯 ASCII（避免 GBK 乱码）

退出码 0 = 全绿。
"""
from __future__ import annotations

import hashlib
import os
import shutil
import sys
import tempfile
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from offerflow_desktop.services import updater  # noqa: E402

FAILS: list = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name}  {detail}")
        FAILS.append(name)


def sha_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def sha_file(p: str) -> str:
    return hashlib.sha256(open(p, "rb").read()).hexdigest()


def write(path: str, data: bytes) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(data)


def main() -> int:
    tmp = tempfile.mkdtemp(prefix="of-inc-test-")
    root = os.path.join(tmp, "install")           # 假装的安装目录
    pool = os.path.join(tmp, "pool")              # 服务器内容池根
    staging = os.path.join(tmp, "staging")

    # ---- 安装目录现状 ----
    a_data = b"AAAA-unchanged-content" * 100
    b_old = b"BBBB-old-content" * 100
    b_new = b"BBBB-NEW-content-v2" * 100
    n_data = b"NNNN-brand-new-file" * 100
    write(os.path.join(root, "a.txt"), a_data)
    write(os.path.join(root, "sub", "b.dll"), b_old)
    write(os.path.join(root, "old.txt"), b"obsolete")

    a_sha, bnew_sha, n_sha = sha_bytes(a_data), sha_bytes(b_new), sha_bytes(n_data)

    # ---- 内容池（服务端）：按 files/<sha[:2]>/<sha> 存放 ----
    for data in (a_data, b_new, n_data):
        h = sha_bytes(data)
        write(os.path.join(pool, "files", h[:2], h), data)

    handler = SimpleHTTPRequestHandler
    httpd = ThreadingHTTPServer(("127.0.0.1", 0),
                                lambda *a, **kw: handler(*a, directory=pool, **kw))
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()

    manifest = {
        "version": "9.9.9",
        "exe": "OfferFlow.exe",
        "base": f"http://127.0.0.1:{port}",
        "files": {
            "a.txt": {"s": len(a_data), "h": a_sha},
            "sub/b.dll": {"s": len(b_new), "h": bnew_sha},
            "new.txt": {"s": len(n_data), "h": n_sha},
        },
        "removed": ["old.txt", "not-exist.txt"],
    }

    print("== 1. 内容寻址地址 ==")
    url = updater.block_url(manifest, bnew_sha)
    check("块地址含 sha[:2] 分片与完整 sha",
          url == f"http://127.0.0.1:{port}/files/{bnew_sha[:2]}/{bnew_sha}", url)

    print("== 2. 增量计划（只挑变化/缺失的文件）==")
    plan = updater.plan_incremental(manifest, root)
    rels = sorted(x["rel"] for x in plan["download"])
    check("仅 sub/b.dll 与 new.txt 需要下载", rels == ["new.txt", "sub/b.dll"], str(rels))
    check("a.txt 内容一致被跳过", plan["unchanged"] == 1, str(plan["unchanged"]))
    check("下载字节数只统计变化文件",
          plan["total_bytes"] == len(b_new) + len(n_data), str(plan["total_bytes"]))
    check("removed 只保留本地真实存在的", plan["remove"] == ["old.txt"], str(plan["remove"]))

    # 反向对照：把 a.txt 的期望 sha 改掉，它就必须重新进入下载列表
    bad = dict(manifest)
    bad["files"] = dict(manifest["files"])
    bad["files"]["a.txt"] = {"s": len(a_data), "h": "0" * 64}
    plan_bad = updater.plan_incremental(bad, root)
    check("反向：哈希对不上则必须重新下载（不是无脑跳过）",
          "a.txt" in [x["rel"] for x in plan_bad["download"]])

    print("== 3. 真下载 + 逐块校验 ==")
    res = updater.stage_updates(manifest, root, staging)
    check("stage_updates 成功", bool(res.get("ok")), str(res.get("error")))
    st_b = os.path.join(staging, "sub", "b.dll")
    st_n = os.path.join(staging, "new.txt")
    check("变化的 b.dll 已落到暂存区且哈希正确",
          os.path.isfile(st_b) and sha_file(st_b) == bnew_sha)
    check("新增的 new.txt 已落到暂存区且哈希正确",
          os.path.isfile(st_n) and sha_file(st_n) == n_sha)
    check("未变化的 a.txt 不进暂存区", not os.path.exists(os.path.join(staging, "a.txt")))
    plan_txt = os.path.join(staging, updater._PLAN_NAME)
    rm_txt = os.path.join(staging, updater._REMOVE_NAME)
    check("生成替换清单 _plan.txt", os.path.isfile(plan_txt))
    check("生成删除清单 _remove.txt",
          os.path.isfile(rm_txt) and open(rm_txt, encoding="utf-8").read().strip() == "old.txt")
    if os.path.isfile(plan_txt):
        body = open(plan_txt, encoding="utf-8").read()
        check("清单含两个待替换文件且带绝对路径",
              "new.txt|" in body and "sub/b.dll|" in body and os.sep in body, body[:120])

    print("== 4. 断点续传（暂存区已有则不重下）==")
    before = os.path.getsize(st_n)
    os.utime(st_n, (0, 0))
    res2 = updater.stage_updates(manifest, root, staging)
    check("二次调用仍成功且文件大小不变", bool(res2.get("ok")) and os.path.getsize(st_n) == before)

    print("== 5. 反向对照：内容与地址 sha 不符必须拒绝 ==")
    evil_sha = "f" * 64
    write(os.path.join(pool, "files", evil_sha[:2], evil_sha), b"TAMPERED-BY-MITM")
    evil_manifest = dict(manifest)
    evil_manifest["files"] = {"evil.txt": {"s": 18, "h": evil_sha}}
    evil_manifest["removed"] = []
    staging2 = os.path.join(tmp, "staging2")
    res3 = updater.stage_updates(evil_manifest, root, staging2)
    check("被篡改的块被拒绝（ok=False）", res3.get("ok") is False, str(res3))
    check("被篡改的块未落盘", not os.path.exists(os.path.join(staging2, "evil.txt")))
    check("错误信息点明校验失败", "校验失败" in str(res3.get("error")), str(res3.get("error")))

    print("== 6. 引导脚本 ==")
    script = updater.write_apply_script(root, staging, manifest, 4242)
    raw = open(script, "rb").read()
    text = raw.decode("utf-8")
    check("bat 已生成", os.path.isfile(script))
    check("bat 使用 CRLF 换行", b"\r\n" in raw and b"\n\n" not in raw)
    check("bat 全 ASCII（GBK 控制台不会乱码）", all(ord(c) < 128 for c in text))
    check("先等待主进程退出（PID 4242）", "4242" in text and "tasklist" in text)
    check("替换用 move /Y 并先改名为 .ofbak", "move /Y" in text and ".ofbak" in text)
    check("失败会回滚（把 .ofbak 移回）", "ROLLBACK" in text)
    check("处理删除清单", "_remove.txt" in text and "del /F /Q" in text)
    check("结束后重新拉起主程序", 'start ""' in text and "OfferFlow.exe" in text)

    print("== 7. 引导脚本的窗口策略（曾用 DETACHED_PROCESS → 用户看到黑窗）==")
    # 背景：DETACHED_PROCESS 说「子进程没有控制台」，但 cmd.exe 是控制台程序，
    # 发现没有就自己新建一个**可见**的 ⇒ 点「自动更新→立即安装」弹黑窗。
    # 这里抓真实传给 Popen 的 creationflags，证明用的是 CREATE_NO_WINDOW。
    import subprocess as _sp

    _orig_popen = _sp.Popen
    _cap: dict = {}

    class _FakePopen:                      # 只记录参数，不起真进程
        def __init__(self, args, **kw):
            _cap["args"] = list(args)
            _cap["flags"] = int(kw.get("creationflags") or 0)
            _cap["kwargs"] = kw

    _sp.Popen = _FakePopen                 # noqa: 临时替换，finally 还原
    try:
        launched = updater.launch_apply_and_exit(script)
    finally:
        _sp.Popen = _orig_popen

    DETACHED = getattr(_sp, "DETACHED_PROCESS", 0x00000008)
    NO_WINDOW = getattr(_sp, "CREATE_NO_WINDOW", 0x08000000)
    got = _cap.get("flags", 0)
    check("launch_apply_and_exit 返回 True", launched is True)
    check("拉起方式为 cmd.exe /c <脚本>",
          _cap.get("args") == ["cmd.exe", "/c", script], str(_cap.get("args")))
    check("★ 未用 DETACHED_PROCESS（用了会让 cmd 自建可见控制台 → 黑窗）",
          not (got & DETACHED), "flags=0x%08X" % got)
    check("★ 用了 CREATE_NO_WINDOW（隐藏控制台；bat 内 tasklist/ping 照常跑）",
          bool(got & NO_WINDOW), "flags=0x%08X" % got)
    _kw = _cap.get("kwargs", {})
    check("stdio 全部丢弃（不挂到父进程控制台）",
          _kw.get("stdout") == _sp.DEVNULL and _kw.get("stderr") == _sp.DEVNULL
          and _kw.get("stdin") == _sp.DEVNULL)

    httpd.shutdown()
    shutil.rmtree(tmp, ignore_errors=True)

    print()
    if FAILS:
        print(f"增量更新测试未通过：{len(FAILS)} 项")
        for f in FAILS:
            print("  -", f)
        return 1
    print("增量更新测试全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
