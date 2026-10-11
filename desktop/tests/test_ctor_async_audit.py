"""审计：找出「在 __init__ 里直接发起异步请求」的 UI 类。

风险：构造期起后台线程，若该控件在请求返回前被销毁（退出登录后重建主窗口、
页面被 deleteLater、切换页面），后台线程的回调会摸到已删除的 C++ 对象 ——
进程直接崩溃，既没有 Python 异常也没有 traceback，极难排查（v2.2.7 实测踩过：
AdminPage 的 ContentTab/UsersTab 在构造期拉数据，第二次构造主窗口时必崩）。

正确做法：构造期只搭界面，数据加载放到 showEvent / Tab 切换 / 按钮回调里。

注意：只统计「构造期真的会执行」的调用 —— lambda / 嵌套函数里的调用是回调，
构造期不会执行，不算问题（这是本脚本最容易误报的地方）。

运行：python tests/test_ctor_async_audit.py
"""

import ast
import os
import sys

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
PKG = os.path.join(os.path.abspath(ROOT), "offerflow_desktop")

# 构造期直接调用即视为「发请求」的方法名（前缀匹配）
_ASYNC_PREFIXES = ("_load", "_refresh", "_reload", "_fetch", "_sync", "_pull")
_ASYNC_EXACT = {"load", "_async"}

# 已知安全的构造期调用：**只读本地 SQLite / 本地配置**，不起线程、不发网络。
# 逐条人工确认过（走 db.* 或本地配置，没有 LLMWorker / api_client 调用）。
# ⚠️ 若哪天这些方法内部改成了走云端接口，必须从这里删掉，否则审计会漏报。
_SAFE = {
    ("ui/login.py", "LoginDialog", "_load_remembered"),
    ("ui/pages/interview.py", "InterviewPage", "_refresh_articles"),
    ("ui/pages/jd_analysis.py", "JDAnalysisPage", "_refresh_table"),
    ("ui/pages/match.py", "MatchPage", "_refresh"),
    ("ui/pages/system_settings.py", "SystemSettingsPage", "_load"),
    ("ui/pages/tracker.py", "TrackerPage", "_refresh"),
    # 本地模型配置对话框：_load 只读本地 SQLite（database.get_local_model_config），
    # 不起线程、不联网。护栏见 test_local_model_config.py 的
    # test_local_model_dialog_load_stays_local —— 哪天它变成联网调用，那条会红。
    ("ui/local_model_dialog.py", "LocalModelDialog", "_load"),
}


class _InitVisitor(ast.NodeVisitor):
    """收集 __init__ 体内**会执行**的「发请求」调用（不进入 lambda / 嵌套函数）。"""

    def __init__(self):
        self.hits = []

    def visit_Lambda(self, node):
        return  # lambda 体是回调，构造期不执行

    def visit_FunctionDef(self, node):
        return  # 嵌套函数同理；__init__ 自身由调用方直接遍历 body

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_Call(self, node):
        func = node.func
        if isinstance(func, ast.Attribute):
            name = func.attr
        elif isinstance(func, ast.Name):
            name = func.id
        else:
            name = ""
        if name in _ASYNC_EXACT or any(name.startswith(p) for p in _ASYNC_PREFIXES):
            self.hits.append((node.lineno, name))
        self.generic_visit(node)


def audit() -> list:
    problems = []
    for dirpath, _dirnames, filenames in os.walk(PKG):
        if "__pycache__" in dirpath:
            continue
        for filename in sorted(filenames):
            if not filename.endswith(".py"):
                continue
            path = os.path.join(dirpath, filename)
            rel = os.path.relpath(path, PKG).replace("\\", "/")
            with open(path, encoding="utf-8") as fh:
                tree = ast.parse(fh.read(), path)
            for cls in [n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)]:
                for fn in cls.body:
                    if not (isinstance(fn, ast.FunctionDef) and fn.name == "__init__"):
                        continue
                    visitor = _InitVisitor()
                    for stmt in fn.body:
                        visitor.visit(stmt)
                    for lineno, name in visitor.hits:
                        if (rel, cls.name, name) in _SAFE:
                            continue
                        problems.append((path, lineno, cls.name, name))
    return problems


def main() -> int:
    problems = audit()
    if not problems:
        print("构造期异步审计通过：没有在 __init__ 里直接发起请求")
        return 0
    print("发现 %d 处「构造期直接发请求」（有崩溃风险）：" % len(problems))
    for path, lineno, cls, name in problems:
        print("  %s:%s  %s.__init__ -> %s()" % (os.path.relpath(path, ROOT), lineno, cls, name))
    print("\n修法：把这次调用挪到 showEvent / 按钮回调里，构造期只搭界面。")
    return 1


if __name__ == "__main__":
    sys.exit(main())
