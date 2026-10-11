#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""离屏渲染「简历优化 · 前后对比」页，出图供肉眼核验（浅色 / 深色 / 分数下降 / 无数据）。

用途：`test_resume_optimize_ats.py` 管「分数对不对、量程对不对、显隐对不对」，
这张图管「看起来对不对」—— 两条条形的长短比例、差值文案的位置、
两份结论列表的排布、关键词三态标签是否一眼能分。

⚠ 离屏（offscreen）平台下**中文字体可能缺失**，文字会画成方块。
这不是页面的问题，是渲染后端的问题；看布局/配色/比例仍然有效。

运行：python tests/_verify_ats_shot.py [输出目录]
"""

import os
import sys
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from PySide6.QtWidgets import QApplication, QWidget

_APP = QApplication.instance() or QApplication([])

from offerflow_desktop.ui import theme
from offerflow_desktop.ui.pages import resume_optimize as ro

RESUMES = [{"id": "r1", "raw_text": "张三\nPython 后端\n教育：某大学 计算机",
            "parsed_json": {"position": "Python 后端"}, "version": 2}]
JDS = [{"id": "j1", "company": "合合信息", "position": "RAG 算法",
        "raw_text": "熟悉 Python、RAG、向量检索；了解 K8s"}]

ATS_BEFORE = {
    "score": 62,
    "issues": ["缺少手机号联系方式", "存在超长段落，建议拆分为短句和项目符号",
               "JD 关键词命中率仅 40%，ATS 可能无法识别相关技能"],
    "suggestions": ["将大段描述改为一句话要点 + 数据化成果"],
    "matched_keywords": ["Python"], "missing_keywords": ["RAG", "向量检索"],
}
ATS_AFTER = {
    "score": 88,
    "issues": [],
    "suggestions": ["补充 JD 中的关键词：K8s"],
    "matched_keywords": ["Python", "RAG", "向量检索"], "missing_keywords": ["K8s"],
}
GAP = {"matched": ["Python", "RAG", "向量检索"], "partial": ["Docker"],
       "missing": ["K8s", "Go"]}


class _FakeMW(QWidget):
    def refresh_status(self):
        pass

    def navigate(self, *a, **k):
        pass

    def open_agent(self, *a, **k):
        pass


def render(out_dir: str) -> None:
    os.makedirs(out_dir, exist_ok=True)
    with mock.patch.object(ro.db, "list_resumes", lambda: [dict(r) for r in RESUMES]), \
            mock.patch.object(ro.db, "list_jds", lambda: [dict(j) for j in JDS]), \
            mock.patch.object(ro, "QMessageBox"):
        page = ro.ResumeOptimizePage(_FakeMW())
        page.resize(1280, 860)
        page.before_view.setPlainText(RESUMES[0]["raw_text"])
        page.after_view.setPlainText("张三\nPython 后端工程师\n技能：Python、RAG")

        shots = [
            ("ATS-提升", ATS_BEFORE, ATS_AFTER, "light", theme.MAIN_QSS),
            ("ATS-提升-dark", ATS_BEFORE, ATS_AFTER, "dark", theme.DARK_QSS),
            ("ATS-下降", ATS_AFTER, ATS_BEFORE, "light", theme.MAIN_QSS),
        ]
        for name, before, after, tone, qss in shots:
            _APP.setStyleSheet(qss)
            ro.tokens.set_theme(tone)
            page._render_ats(before, after, GAP)
            page.show()
            page.tabs.setCurrentIndex(0)          # 前后对比 Tab
            _APP.processEvents()
            path = os.path.join(out_dir, "%s.png" % name)
            page.grab().save(path)
            print("已写出", path)

        # 无数据：整块该收起来（不能画出一根「0 分」的条）
        ro.tokens.set_theme("light")
        _APP.setStyleSheet(theme.MAIN_QSS)
        page._render_ats(None, None, GAP)
        _APP.processEvents()
        path = os.path.join(out_dir, "ATS-无数据（整块收起）.png")
        page.grab().save(path)
        print("已写出", path, "卡片可见 =", not page.ats_card.isHidden())


if __name__ == "__main__":
    render(sys.argv[1] if len(sys.argv) > 1 else os.path.join(_ROOT, "_out", "ats"))
