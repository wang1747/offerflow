"""自动更新完整性校验（审计 P1-4）回归测试——可直接 python 运行，不依赖 pytest。

验证三件事：
1. verify_file_sha256：正确哈希 → True；错误哈希 / 空期望 → False。
2. download_installer(expected_sha256=...)：哈希匹配则成功；不匹配则抛
   ValueError 且落地文件被删除（绝不当成可用安装包）。不联网，用 monkeypatch
   伪造 requests.get 的流式响应。
3. check_for_update：把 version.json 的 sha256 字段透传到返回的 info 字典。
"""

import hashlib
import os
import sys
import tempfile

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from offerflow_desktop.services import updater
import offerflow_desktop.services.cloud as _cloud_mod


class _FakeResp:
    """伪造 requests 的流式响应（支持 `with` 与 iter_content）。"""

    def __init__(self, data: bytes):
        self._data = data
        self.headers = {"Content-Length": str(len(data))}

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def raise_for_status(self):
        return None

    def iter_content(self, chunk_size=1024):
        for i in range(0, len(self._data), chunk_size):
            yield self._data[i:i + chunk_size]


fails = []


def check(cond, label, extra=""):
    print(("OK  " if cond else "XX  ") + label + ("  " + str(extra) if extra else ""))
    if not cond:
        fails.append(label)


def _patch(target, name, value):
    orig = getattr(target, name)
    setattr(target, name, value)
    return (target, name, orig)


def _restore(p):
    setattr(p[0], p[1], p[2])


def main() -> int:
    # ---- 1. verify_file_sha256 ----
    payload = b"OfferFlow-Setup-FAKE-EXE-BINARY"
    digest = hashlib.sha256(payload).hexdigest()
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "setup.exe")
        with open(p, "wb") as f:
            f.write(payload)
        check(updater.verify_file_sha256(p, digest) is True, "正确哈希 → True")
        check(updater.verify_file_sha256(p, digest.upper()) is True,
              "哈希大小写不敏感")
        check(updater.verify_file_sha256(p, "deadbeef") is False, "错误哈希 → False")
        check(updater.verify_file_sha256(p, "") is False, "空期望 → False")
        check(updater.verify_file_sha256(os.path.join(d, "nope.exe"), digest) is False,
              "文件不存在 → False")

    # ---- 2. download_installer 正确哈希 ----
    payload2 = b"ABC" * 5000
    digest2 = hashlib.sha256(payload2).hexdigest()
    rp = _patch(updater.requests, "get", lambda *a, **k: _FakeResp(payload2))
    with tempfile.TemporaryDirectory() as d:
        dest = os.path.join(d, "OfferFlow-Setup-9.9.9.exe")
        ok = updater.download_installer(
            "http://x/setup.exe", dest, expected_sha256=digest2)
        check(ok is True, "download_installer 正确哈希 → 成功")
        check(os.path.exists(dest), "正确哈希时文件落地")
        with open(dest, "rb") as f:
            check(f.read() == payload2, "落地内容完整")
    _restore(rp)

    # ---- 3. download_installer 错误哈希：抛异常 + 删文件 ----
    rp = _patch(updater.requests, "get", lambda *a, **k: _FakeResp(b"tampered"))
    with tempfile.TemporaryDirectory() as d:
        dest = os.path.join(d, "OfferFlow-Setup-9.9.9.exe")
        raised = False
        try:
            updater.download_installer(
                "http://x/setup.exe", dest, expected_sha256="0" * 64)
        except ValueError as e:
            raised = True
            check("sha256" in str(e).lower(), "错误哈希抛 ValueError 且含 sha256 字样")
        check(raised, "错误哈希必须抛 ValueError")
        check(not os.path.exists(dest), "校验失败的安装包必须被删除")
    _restore(rp)

    # ---- 3b. download_installer 缺失 sha256：必须拒绝（审计复核，杜绝删字段绕过）----
    rp = _patch(updater.requests, "get", lambda *a, **k: _FakeResp(b"payload"))
    with tempfile.TemporaryDirectory() as d:
        dest = os.path.join(d, "OfferFlow-Setup-9.9.9.exe")
        raised = False
        try:
            updater.download_installer("http://x/setup.exe", dest, expected_sha256=None)
        except ValueError as e:
            raised = True
            check("sha256" in str(e), "缺失 sha256 抛 ValueError 且含 sha256 字样")
        check(raised, "缺失 sha256 必须抛 ValueError（不允许跳过校验）")
        check(not os.path.exists(dest), "缺失 sha256 时安装包不得落地")
    _restore(rp)

    # ---- 4. check_for_update 透传 sha256 ----
    info_json = {
        "version": "99.0.0",
        "notes": "security: sha256 enforced",
        "download_url": "/downloads/OfferFlow-Setup.exe",
        "sha256": "a" * 64,
    }

    class _Resp:
        def raise_for_status(self):
            return None

        def json(self):
            return info_json

    rp1 = _patch(updater.requests, "get", lambda *a, **k: _Resp())
    rp2 = _patch(_cloud_mod, "get_cloud_url", lambda: "http://example")
    out = updater.check_for_update()
    _restore(rp1)
    _restore(rp2)
    check(out is not None, "check_for_update 返回更新信息")
    check(out["latest"] == "99.0.0", "透传 latest 版本")
    check(out["sha256"] == "a" * 64, "透传 sha256 字段")
    check(out["download_url"].startswith("http://example"), "download_url 补全 base")

    print()
    if fails:
        print("FAILED: %d 处 -> %s" % (len(fails), "; ".join(fails)))
        return 1
    print("ALL OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
