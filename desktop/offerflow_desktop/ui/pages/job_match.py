"""岗位分析页：把「职位要求分析」与「岗位匹配」合并为一个带 Tab 的单页（对齐网页版 JobMatch）。

- Tab 1「解析职位」：内嵌 JDAnalysisPage
- Tab 2「匹配度」：内嵌 MatchPage

两个子页保留完整能力，仅把之前指向「另一个页面」的提示语改成指向对应标签页。
子页构造时传 ``with_title=False``，避免与外层「岗位分析」标题重复。
"""

from PySide6.QtWidgets import QTabWidget, QVBoxLayout, QWidget

from ..components import make_title
from .jd_analysis import JDAnalysisPage
from .match import MatchPage


class JobMatchPage(QWidget):
    def __init__(self, main_window):
        super().__init__()
        self.mw = main_window

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.setSpacing(12)
        layout.addWidget(make_title(
            "岗位分析",
            "解析岗位要求，再算简历匹配度。",
        ))

        self.tabs = QTabWidget()
        self.tabs.setDocumentMode(True)

        # 子页保留完整逻辑，仅隐藏各自的顶层标题（避免与「岗位分析」重复）
        self.jd_page = JDAnalysisPage(self.mw, with_title=False)
        self.match_page = MatchPage(self.mw, with_title=False)
        self.tabs.addTab(self.jd_page, "解析职位")
        self.tabs.addTab(self.match_page, "匹配度")

        layout.addWidget(self.tabs, 1)

    def _refresh(self) -> None:
        """切到本页时刷新两个子页（防御式调用：子页方法名变动也不致崩溃）。"""
        for page in (self.jd_page, self.match_page):
            fn = getattr(page, "_refresh", None)
            if not callable(fn):
                fn = getattr(page, "_refresh_table", None)
            if callable(fn):
                try:
                    fn()
                except Exception:  # noqa: BLE001
                    pass
