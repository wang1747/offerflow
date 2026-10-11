"""量化两个构建目录之间的文件级差异：增量更新的真实收益。

输出：未变/变化/新增/删除 的文件数与字节数，以及「增量下载量 vs 全量下载量」。
"""
from __future__ import annotations

import hashlib
import os
import sys


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def scan(root: str) -> dict:
    out = {}
    for dirpath, _dirnames, filenames in os.walk(root):
        for name in filenames:
            full = os.path.join(dirpath, name)
            rel = os.path.relpath(full, root).replace("\\", "/")
            try:
                out[rel] = (os.path.getsize(full), sha256_file(full))
            except OSError:
                pass
    return out


def main() -> int:
    old_root, new_root = sys.argv[1], sys.argv[2]
    old = scan(old_root)
    new = scan(new_root)

    same = [r for r in new if r in old and old[r][1] == new[r][1]]
    changed = [r for r in new if r in old and old[r][1] != new[r][1]]
    added = [r for r in new if r not in old]
    removed = [r for r in old if r not in new]

    def total(m: dict, keys) -> int:
        return sum(m[k][0] for k in keys)

    inc_bytes = total(new, changed) + total(new, added)
    full_bytes = total(new, new.keys())

    print(f"旧构建: {old_root}  文件 {len(old)}")
    print(f"新构建: {new_root}  文件 {len(new)}")
    print("")
    print(f"未变  : {len(same):4d} 个  {total(new, same) / 1048576:8.1f} MB")
    print(f"变化  : {len(changed):4d} 个  {total(new, changed) / 1048576:8.1f} MB")
    print(f"新增  : {len(added):4d} 个  {total(new, added) / 1048576:8.1f} MB")
    print(f"删除  : {len(removed):4d} 个  {total(old, removed) / 1048576:8.1f} MB")
    print("")
    print(f"增量下载量 : {inc_bytes / 1048576:.1f} MB ({inc_bytes} bytes)")
    print(f"全量下载量 : {full_bytes / 1048576:.1f} MB ({full_bytes} bytes)")
    if full_bytes:
        print(f"节省       : {100.0 * (1 - inc_bytes / full_bytes):.1f}%")
    print("")
    print("--- 变化/新增文件明细（按体积降序 TOP 20）---")
    for r in sorted(changed + added, key=lambda k: -new[k][0])[:20]:
        tag = "CHG" if r in changed else "NEW"
        print(f"  {tag}  {new[r][0] / 1048576:8.2f} MB  {r}")
    print("")
    print(f"--- 主 exe 是否变化 ---")
    if "OfferFlow.exe" in new and "OfferFlow.exe" in old:
        same_exe = old["OfferFlow.exe"][1] == new["OfferFlow.exe"][1]
        print(f"  OfferFlow.exe 哈希相同: {same_exe}  (决定能否绕开「替换运行中的 exe」)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
