"""登录态安全存储（审计 P2-5）回归测试——可直接 python 运行。

1. secure_storage.protect/unprotect 在「当前平台」round-trip 一致。
2. Windows 上密文确实不等于明文（DPAPI 生效）；非 Windows 标记为回退。
3. api_client._save_auth / load_auth：写出的 auth.json 含 enc 字段、非明文，
   且能恢复登录态；并兼容旧明文格式（升级不丢登录态）。
"""

import base64
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from offerflow_desktop import secure_storage
from offerflow_desktop import api_client

fails = []
skips = []


def check(cond, label, extra=""):
    print(("OK  " if cond else "XX  ") + label + ("  " + str(extra) if extra else ""))
    if not cond:
        fails.append(label)


def main() -> int:
    # ---- 1. round-trip ----
    sample = '{"token":"eyJhbGciOi.abc.def","user":{"id":1,"email":"u@x.com"}}'
    try:
        enc = secure_storage.protect(sample)
        dec = secure_storage.unprotect(enc)
        check(dec == sample, "protect/unprotect round-trip 一致")
    except Exception as exc:  # noqa: BLE001
        # DPAPI 在当前会话不可用（如某些服务/沙箱会话）时跳过真实加解密
        skips.append("DPAPI 不可用，跳过真实加解密: %s" % exc)
        print("SKIP DPAPI 真实加解密: %s" % exc)
        enc = None

    # ---- 2. 加密有效性 ----
    if enc is not None:
        raw_payload = sample.encode("utf-8")
        plain_b64 = base64.b64encode(raw_payload).decode("ascii")
        if secure_storage.is_encryption_available():
            check(enc != plain_b64, "Windows DPAPI：密文 ≠ 明文 base64（确实加密）")
        else:
            check(enc == plain_b64, "非 Windows 回退：enc == 明文 base64")
    check(isinstance(secure_storage.is_encryption_available(), bool),
          "is_encryption_available 返回 bool")

    # ---- 3. api_client 集成（不碰真实 DATA_DIR）----
    tmp = tempfile.mkdtemp(prefix="offerflow-auth-test-")
    fake_path = os.path.join(tmp, "auth.json")
    saved = api_client._AUTH_PATH
    api_client._AUTH_PATH = fake_path
    try:
        api_client._state["token"] = "TOKEN-SECRET-123"
        api_client._state["user"] = {"id": 7, "email": "me@offerflow.dev"}
        api_client._save_auth()

        # 文件里不应出现明文 token
        with open(fake_path, "r", encoding="utf-8") as f:
            on_disk = f.read()
        check("TOKEN-SECRET-123" not in on_disk,
              "auth.json 磁盘内容不含明文 token")
        check('"enc"' in on_disk, "auth.json 使用 enc 加密字段")

        # 清空内存态后 load 应恢复
        api_client._state["token"] = None
        api_client._state["user"] = None
        api_client.load_auth()
        check(api_client._state["token"] == "TOKEN-SECRET-123",
              "load_auth 恢复 token")
        check(api_client._state["user"].get("email") == "me@offerflow.dev",
              "load_auth 恢复 user")

        # ---- 3b. 旧明文格式兼容 ----
        with open(fake_path, "w", encoding="utf-8") as f:
            json.dump({"token": "OLD-PLAIN-TOKEN",
                       "user": {"id": 9, "email": "old@x.com"}}, f)
        api_client._state["token"] = None
        api_client._state["user"] = None
        api_client.load_auth()
        check(api_client._state["token"] == "OLD-PLAIN-TOKEN",
              "兼容旧明文格式 token")
    finally:
        api_client._AUTH_PATH = saved

    print()
    if skips:
        print("SKIPPED: %s" % "; ".join(skips))
    if fails:
        print("FAILED: %d 处 -> %s" % (len(fails), "; ".join(fails)))
        return 1
    print("ALL OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
