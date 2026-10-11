"""全页面冒烟测试：把全部功能页逐个构造一遍。

构造期就会暴露的问题（Signals 未定义、控件缺失、方法名拼错）在这里能全部抓到，
比「打开应用手点一遍」快得多，也不需要真实显示器（走 offscreen 平台）。

末尾有一道自检：`ui/pages/` 下每个页面文件都必须在 PAGES 里登记，
否则新增页面时容易漏测（v2.2.3 的「交流中心」就这么漏掉过）。

运行：python tests/test_pages_smoke.py
"""

import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, os.path.abspath(ROOT))

from PySide6.QtWidgets import QApplication, QWidget          # noqa: E402
import importlib                                             # noqa: E402

PAGES = [
    ("dashboard", "DashboardPage"),
    ("jd_analysis", "JDAnalysisPage"),
    ("job_match", "JobMatchPage"),
    ("resume_generate", "ResumeGeneratePage"),
    ("resume_optimize", "ResumeOptimizePage"),
    ("match", "MatchPage"),
    ("interview", "InterviewPage"),
    ("salary", "SalaryPage"),
    ("tracker", "TrackerPage"),
    ("community", "CommunityPage"),
    ("cost", "CostPage"),
    ("model_settings", "ModelSettingsPage"),
    ("local_kb", "LocalKnowledgeBasePage"),
    ("system_settings", "SystemSettingsPage"),
    ("admin", "AdminPage"),
    ("agent", "AgentPage"),
]

# 功能页启动时用到的核心能力（计算匹配度 / 生成简历等）也必须能导入
SERVICES = [
    ("offerflow_desktop.services.match", "calculate_match"),
    ("offerflow_desktop.services.resume_generation", "revalidate"),
    ("offerflow_desktop.services.resume_optimizer", "optimize_resume_text"),
    ("offerflow_desktop.core.semantic", "similarity"),
    ("offerflow_desktop.core.continuity", "evaluate_continuity"),
    ("offerflow_desktop.core.expression_bank", "enrich"),
    ("offerflow_desktop.services.templates", "get_template"),
    ("offerflow_desktop.services.pdf_exporter", "render_pdf_bytes"),
    ("offerflow_desktop.services.exporter", "export_docx_from_blocks"),
]

# 登录类窗口不在 ui/pages/ 下，覆盖自检扫不到；但它们挂了应用就直接起不来
DIALOGS = [
    ("login", "LoginDialog"),
    ("forgot_password", "ForgotPasswordDialog"),
    ("change_email", "ChangeEmailDialog"),
]

app = QApplication.instance() or QApplication([])


class _FakeMainWindow(QWidget):
    """只提供功能页构造时需要的宿主接口。"""

    def refresh_status(self):
        pass

    def refresh_ai_source(self):
        pass

    def navigate(self, *args, **kwargs):
        pass

    def set_status(self, *args, **kwargs):
        pass


def _check_page_coverage() -> list:
    """自检：ui/pages/ 下的页面文件都必须登记进 PAGES，防止新增页面漏测。"""
    pages_dir = os.path.join(os.path.abspath(ROOT), "offerflow_desktop", "ui", "pages")
    registered = {name for name, _ in PAGES}
    missing = []
    for filename in sorted(os.listdir(pages_dir)):
        if not filename.endswith(".py") or filename == "__init__.py":
            continue
        stem = filename[:-3]
        if stem not in registered:
            missing.append(stem)
    return missing


def main() -> int:
    failures = []
    mw = _FakeMainWindow()

    for module_name, class_name in PAGES:
        try:
            module = importlib.import_module("offerflow_desktop.ui.pages.%s" % module_name)
            cls = getattr(module, class_name)
            cls(mw)
            print("OK  页面 %-18s %s" % (module_name, class_name))
        except Exception as exc:                      # noqa: BLE001
            failures.append("%s.%s -> %s: %s" % (module_name, class_name, type(exc).__name__, exc))
            print("XX  页面 %-18s %s: %s" % (module_name, type(exc).__name__, exc))

    missing = _check_page_coverage()
    if missing:
        failures.append("ui/pages/ 下这些页面未登记进 PAGES: %s" % ", ".join(missing))
        print("XX  页面覆盖自检：未登记 -> %s" % ", ".join(missing))
    else:
        print("OK  页面覆盖自检：%d 个页面文件全部已登记" % len(PAGES))

    for module_name, class_name in DIALOGS:
        try:
            module = importlib.import_module("offerflow_desktop.ui.%s" % module_name)
            getattr(module, class_name)()
            print("OK  窗口 %-18s %s" % (module_name, class_name))
        except Exception as exc:                      # noqa: BLE001
            failures.append("%s.%s -> %s: %s" % (module_name, class_name, type(exc).__name__, exc))
            print("XX  窗口 %-18s %s: %s" % (module_name, type(exc).__name__, exc))

    for module_name, attr in SERVICES:
        try:
            module = importlib.import_module(module_name)
            getattr(module, attr)
            print("OK  服务 %-45s .%s" % (module_name, attr))
        except Exception as exc:                      # noqa: BLE001
            failures.append("%s.%s -> %s: %s" % (module_name, attr, type(exc).__name__, exc))
            print("XX  服务 %-45s %s: %s" % (module_name, type(exc).__name__, exc))

    print()
    if failures:
        print("FAILED: %d 处" % len(failures))
        for item in failures:
            print("  -", item)
        return 1
    print("ALL OK（%d 个页面 + %d 个窗口 + %d 个服务）"
          % (len(PAGES), len(DIALOGS), len(SERVICES)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
