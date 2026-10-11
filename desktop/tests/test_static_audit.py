"""静态审计：找出「用了某个名字却没导入/没定义」这类只在运行时才炸的 NameError。

背景：本项目多次出现同一类缺陷 —— 某个页面的按钮回调用了 ``LLMWorker`` 却漏了
import，编译、导入、构造窗口**全都不报错**，直到用户真的点上那个按钮才崩。
这个测试用 AST 把「在 Load 位置出现、但模块内没有任何绑定」的名字全部揪出来。

⚠️ 判据的范围（2026-10-06 修正，这是一次真实漏检换来的）：

    原判据只查 ``node.id[:1].isupper()``（首字母大写的名字），理由是"函数名漏定义
    的情况较少、全量扫描噪音大"。**这个假设是错的**：``ui/main_window.py`` 里
    ``setup_tray()`` 用了 ``app_icon(32)`` 而 import 被误删 —— ``app_icon`` 是小写，
    天生落在盲区里，静态审计照样全绿。它只在 ``main.py`` 登录成功后才会被执行，
    于是发出去的 v2.2.24 表现为「登录完成就闪退」。

    实测：去掉大写过滤、全量扫描整个包，命中数 **1 处（就是上面那个真 bug）、零噪音**。
    所以现在做全量扫描，不再依赖"大写"这个不可靠的启发式。

另外内建了**反向对照自检**（见 ``_self_check``）：审计"通过"有两种可能 —— 真的没问题，
或者检查器本身坏了。每次运行都会先对一个故意写错的样例断言"必须报出来"，
避免交付一个永远输出通过的假护栏。

运行：python tests/test_static_audit.py
"""

import ast
import builtins
import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
PKG = os.path.join(ROOT, "offerflow_desktop")
# 入口脚本也要审：main.py 里的启动链路（构造主窗口 → setup_tray → …）
# 恰恰是"构造测试覆盖不到、真跑才炸"的那一段。
ENTRY_SCRIPTS = [os.path.join(ROOT, "main.py")]

_BUILTINS = set(dir(builtins))
_IMPLICIT = {
    "self", "cls", "__name__", "__file__", "__doc__", "__package__",
    "__spec__", "__loader__", "__builtins__", "__debug__",
}


def _bound_names(tree: ast.AST) -> set:
    """收集模块内所有算作"已绑定"的名字（不分层级，宁可宽松也不要假阳性）。"""
    bound = set(_BUILTINS) | set(_IMPLICIT)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                bound.add((alias.asname or alias.name).split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                bound.add(alias.asname or alias.name)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            bound.add(node.name)
        elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            bound.add(node.id)
        elif isinstance(node, ast.arg):
            bound.add(node.arg)
        elif isinstance(node, ast.ExceptHandler) and node.name:
            bound.add(node.name)
        elif isinstance(node, (ast.Global, ast.Nonlocal)):
            bound.update(node.names)
        elif isinstance(node, (ast.MatchAs, ast.MatchStar)) and node.name:
            # `case Foo(x):` / `case [a, *rest]:` 的捕获名既不是 Store 也不是 arg，
            # 不单独收就会被误报。本项目当前没用 match，留着防以后踩。
            bound.add(node.name)
        elif isinstance(node, ast.MatchMapping) and node.rest:
            bound.add(node.rest)
    return bound


def audit_source(source: str, path: str) -> list:
    """返回 [(path, lineno, name)]。"""
    try:
        tree = ast.parse(source, path)
    except SyntaxError as exc:
        return [(path, exc.lineno or 0, "语法错误: %s" % exc)]
    bound = _bound_names(tree)
    problems = []
    for node in ast.walk(tree):
        if (isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load)
                and node.id not in bound):
            problems.append((path, node.lineno, node.id))
    return problems


def _targets() -> list:
    """要审的文件清单：整个包 + 入口脚本（不含 __pycache__ 与一次性调试脚本）。"""
    files = []
    for dirpath, _dirnames, filenames in os.walk(PKG):
        if "__pycache__" in dirpath:
            continue
        for filename in filenames:
            if filename.endswith(".py"):
                files.append(os.path.join(dirpath, filename))
    for entry in ENTRY_SCRIPTS:
        if os.path.isfile(entry):
            files.append(entry)
    return files


def scan(files: list) -> list:
    problems = []
    for path in files:
        with open(path, encoding="utf-8") as fh:
            problems.extend(audit_source(fh.read(), path))
    return problems


def _self_check() -> list:
    """反向对照：故意写一份有缺陷的样例，审计必须报出来。

    没有这一步，"审计通过"与"检查器坏了"在报告里长得一模一样。
    """
    failures = []
    bad = (
        "from .icons import nav_icon\n"
        "def boot():\n"
        "    self.tray = QSystemTrayIcon(app_icon(32))\n"   # app_icon 未导入（小写！）
        "    return nav_icon('home')\n"                      # 已导入，不该报
    )
    hits = [n for _p, _l, n in audit_source(bad, "<probe>")]
    if "app_icon" not in hits:
        failures.append("反向对照失败：漏定义的小写名字 app_icon 未被报出")
    if "nav_icon" in hits:
        failures.append("反向对照失败：已导入的 nav_icon 被误报")
    if "QSystemTrayIcon" not in hits:
        failures.append("反向对照失败：未导入的大写名字 QSystemTrayIcon 未被报出")
    return failures


def main() -> int:
    rc = 0

    print("=== 反向对照自检（确认检查器本身有效）===")
    self_failures = _self_check()
    if self_failures:
        for msg in self_failures:
            print("  XX  " + msg)
        rc = 1
    else:
        print("  OK  样例里的漏定义名字被报出、已导入名字未被误报")

    print("\n=== 全量扫描（%d 个文件，含入口脚本）===" % len(_targets()))
    problems = scan(_targets())
    if not problems:
        print("  OK  没有发现未导入/未定义的名字引用")
    else:
        rc = 1
        print("  发现 %d 处未导入/未定义的名字引用：" % len(problems))
        for path, lineno, name in problems:
            print("    %s:%s  %s" % (os.path.relpath(path, ROOT), lineno, name))

    print("\n%s" % ("静态审计失败" if rc else "静态审计通过"))
    return rc


if __name__ == "__main__":
    sys.exit(main())
