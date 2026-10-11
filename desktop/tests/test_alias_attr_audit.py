"""别名属性审计：找出「从 A 导入并起别名，却调用 A 里不存在的函数」这类崩溃。

背景（真实事故）：桌面版从「本地 SQLite 存数据」迁到「云端 API 存数据」时，
各页面把 `from ... import database as db` 改成了 `from ... import api_client as db`，
但有三处调用没跟着改：
    resume_generate.py:  db.save_resume(...)        # api_client 里没有
    resume_generate.py:  db.resolve_export_dir()    # api_client 里没有
    resume_optimize.py:  db.resolve_export_dir()    # api_client 里没有
后果：点「导入简历」/「导出简历」直接 AttributeError —— 简历既进不了库、
也存不成文件；而编译、导入、页面构造、全页面冒烟测试**全都不会报错**。

判定方式：对**本项目内部模块**的 `import ... as 别名`，收集目标模块的顶层名字，
再用 AST 检查每个 `别名.属性` 是否是真实存在的顶层名。
用 AST 而不是正则，天然不会把字符串/注释里的 `db.save_resume(...)` 误判。

⚠️ AST 坑：`from ... import api_client as db` 会被解析成
`ImportFrom(module=None, level=3, names=[alias('api_client','db')])`
—— 包路径藏在 **alias.name** 里，不是 module。一开始按 module 解析，
结果所有目标都被当成 `offerflow_desktop/__init__.py`，报出 170 处假问题。

运行：python tests/test_alias_attr_audit.py
"""

import ast
import os
import sys

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
PKG = os.path.join(ROOT, "offerflow_desktop")

# 目标模块里若出现这些「动态定义」迹象，一律跳过该模块（静态分析不可靠）
_DYNAMIC_MARKERS = ("__getattr__", "setattr(sys.modules")

# 已知的合理例外：(模块文件名, 属性名)
_ALLOW = set()


def _find_module(base_no_ext: str):
    """把「无扩展名的模块路径」解析成真实文件；找不到返回 None。"""
    for cand in (base_no_ext + ".py", os.path.join(base_no_ext, "__init__.py")):
        if os.path.isfile(cand):
            return os.path.normpath(cand)
    return None


def _resolve_aliases(tree: ast.AST, path: str) -> dict:
    """返回 {本地别名: 目标模块文件路径}，只保留本包内的模块。"""
    out = {}
    here = os.path.dirname(os.path.abspath(path))
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom):
            continue
        level = node.level or 0
        module = node.module or ""

        if level == 0:
            # 绝对导入：只认本包
            if not module.startswith("offerflow_desktop"):
                continue
            base = os.path.join(ROOT, *module.split("."))
            for a in node.names:
                if not a.asname:
                    continue
                cand = _find_module(os.path.join(base, *a.name.split("."))) or _find_module(base)
                if cand:
                    out[a.asname] = cand
            continue

        # 相对导入：从当前文件所在目录向上回溯 level-1 层
        base = here
        for _ in range(level - 1):
            base = os.path.dirname(base)

        if module:
            # from ...config import X   /   from ..services import templates as tpl
            mdir = os.path.join(base, *module.split("."))
            for a in node.names:
                if not a.asname:
                    continue
                cand = _find_module(os.path.join(mdir, *a.name.split("."))) or _find_module(mdir)
                if cand:
                    out[a.asname] = cand
        else:
            # from ... import api_client as db  ← 包路径在 alias.name 上
            for a in node.names:
                if not a.asname:
                    continue
                cand = _find_module(os.path.join(base, *a.name.split(".")))
                if cand:
                    out[a.asname] = cand
    return out


def _top_names(path: str):
    """模块的顶层绑定名（含 re-export 的 import 名字、类、函数、常量）。"""
    with open(path, encoding="utf-8") as fh:
        src = fh.read()
    tree = ast.parse(src, path)
    names = set()
    containers = list(tree.body)
    for node in containers:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name):
                    names.add(t.id)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for a in node.names:
                names.add(a.asname or a.name.split(".")[0])
        elif isinstance(node, (ast.If, ast.Try)):
            # 兼容 if TYPE_CHECKING: / try: ... except ImportError: 里的定义
            for sub in ast.walk(node):
                if isinstance(sub, (ast.FunctionDef, ast.ClassDef)):
                    names.add(sub.name)
                elif isinstance(sub, ast.Assign):
                    for t in sub.targets:
                        if isinstance(t, ast.Name):
                            names.add(t.id)
                elif isinstance(sub, (ast.Import, ast.ImportFrom)):
                    for a in sub.names:
                        names.add(a.asname or a.name.split(".")[0])
    return names, src


def audit() -> list:
    cache = {}          # 模块路径 -> (names, src)
    problems = []

    for dirpath, _dirnames, filenames in os.walk(PKG):
        if "__pycache__" in dirpath:
            continue
        for filename in filenames:
            if not filename.endswith(".py"):
                continue
            path = os.path.join(dirpath, filename)
            with open(path, encoding="utf-8") as fh:
                src = fh.read()
            try:
                tree = ast.parse(src, path)
            except SyntaxError:
                continue

            aliases = _resolve_aliases(tree, path)
            if not aliases:
                continue

            # 目标模块带动态定义 → 跳过（静态看不到）
            for tgt in set(aliases.values()):
                if tgt not in cache:
                    cache[tgt] = _top_names(tgt)
                if any(m in cache[tgt][1] for m in _DYNAMIC_MARKERS):
                    aliases = {k: v for k, v in aliases.items() if v != tgt}

            # 用 AST 找 `别名.属性`（字符串/注释不会进 AST）
            for node in ast.walk(tree):
                if not (isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)):
                    continue
                alias = node.value.id
                if alias not in aliases:
                    continue
                tgt = aliases[alias]
                if tgt not in cache:
                    cache[tgt] = _top_names(tgt)
                if node.attr in cache[tgt][0]:
                    continue
                if (os.path.basename(tgt), node.attr) in _ALLOW:
                    continue
                problems.append((path, node.lineno, "%s.%s" % (alias, node.attr),
                                 os.path.relpath(tgt, ROOT)))
    return problems


def main() -> int:
    problems = audit()
    if not problems:
        print("别名属性审计通过：所有 `别名.属性` 调用都真实存在")
        return 0
    print("发现 %d 处「调用了目标模块里不存在的名字」：" % len(problems))
    for path, lineno, expr, tgt in problems:
        print("  %s:%s  %s   (目标模块 %s)" % (os.path.relpath(path, ROOT), lineno, expr, tgt))
    return 1


if __name__ == "__main__":
    sys.exit(main())
