"""薪资谈判页：市场价参考（Tab1）+ 谈判演练（Tab2）。

对齐网页版 SalaryNegotiation.tsx。Tab1 顶部的「市场数据 vN · YYYY 届」信息条
与「导入权威数据」入口也在这里 —— 市场价由**行情规则**算出来，不是 AI
编的，所以用户必须能看到并且能替换这份行情数据。

⚠️ 这里**没有**「刷新市场数据」按钮：那条路在两版都是硬拒绝（模型估算会编造数字
污染真实行情，`api_client.salary_benchmark_refresh` 固定抛错），留一个点了必然
报错的按钮只会让用户以为坏了。要换行情只能「导入权威数据」。
"""

import json
from typing import Any, Dict, Tuple

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox, QDialog, QHBoxLayout, QLabel, QLineEdit, QMessageBox,
    QPlainTextEdit, QPushButton, QSplitter, QTabWidget, QTextEdit,
    QVBoxLayout, QWidget,
)
from shiboken6 import isValid

from ... import api_client as db
from ...core.salary_benchmark import locate_offer, parse_offer_amount
from ...llm import LLMWorker
from ...services.salary_negotiation import MAX_ROUNDS as MAX_NEGOTIATION_ROUNDS
from ..components import (BusyOverlay, api_guard, card, empty_state, hint_label,
                          make_title, primary_button, section_label, set_prop)
from ..import_resume import make_import_button, resume_empty_hint

#: 导入基准数据时的必需字段（与 `core.salary_benchmark.sanitize_data` 的口径一致）
BENCHMARK_REQUIRED_FIELDS = ("degree_base", "direction_coef", "city_tiers")


def parse_benchmark_payload(raw: str) -> Tuple[Dict[str, Any] | None, str]:
    """解析并校验待导入的基准数据 JSON。

    返回 ``(data, error)``：通过时 ``(dict, "")``，不通过时 ``(None, 说明)``。
    口径与网页版一致 —— 三个必需字段必须**存在且非空**（空 dict 也算没给）。
    抽成纯函数是为了让判据能直接测校验，不必弹窗、不必联网。
    """
    try:
        parsed = json.loads(raw or "")
    except (ValueError, TypeError):
        return None, "JSON 格式不正确，请检查"
    if not isinstance(parsed, dict):
        return None, "JSON 顶层必须是一个对象（{...}）"
    missing = [k for k in BENCHMARK_REQUIRED_FIELDS if not parsed.get(k)]
    if missing:
        return None, "缺少必要字段：" + " / ".join(missing)
    return parsed, ""


class _BenchmarkImportDialog(QDialog):
    """导入权威薪资基准数据的弹窗（字段口径对齐网页版）。

    ⚠️ 桌面版的基准数据存在**本机**（网页版那份是服务端全局的、两端有意不共享），
    所以文案必须说成「本机生效」——说成「对所有账号生效」会让用户以为网页版也跟着变了。
    """

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("导入权威市场薪资数据")
        self.setMinimumWidth(560)
        self._parsed: Dict[str, Any] | None = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 16)
        layout.setSpacing(10)

        tip = QLabel(
            "拿到权威薪酬报告后，把整理好的基准数据（JSON）粘贴进来，会作为新版本"
            "覆盖当前数据，**仅在本机生效**（网页版那份不受影响）。结构需包含 "
            "degree_base、direction_coef、city_tiers 三个字段。")
        tip.setWordWrap(True)
        tip.setProperty("class", "hint")
        layout.addWidget(tip)

        row = QHBoxLayout()
        row.setSpacing(8)
        row.addWidget(QLabel("数据年份"))
        self.year_edit = QLineEdit(placeholderText="如：2027")
        row.addWidget(self.year_edit, 1)
        row.addWidget(QLabel("数据来源"))
        self.source_edit = QLineEdit(placeholderText="如：某机构 2027 届薪酬报告")
        row.addWidget(self.source_edit, 1)
        layout.addLayout(row)

        layout.addWidget(QLabel("基准数据 JSON"))
        self.json_edit = QPlainTextEdit()
        self.json_edit.setPlaceholderText(
            '{"degree_base": {...}, "direction_coef": {...}, "city_tiers": {...}}')
        self.json_edit.setFixedHeight(200)
        self.json_edit.setStyleSheet("font-family: Consolas, monospace; font-size: 12px;")
        layout.addWidget(self.json_edit)

        self.msg = QLabel("")
        self.msg.setWordWrap(True)
        self.msg.setStyleSheet("color: #dc2626; font-size: 12px;")
        self.msg.setVisible(False)
        layout.addWidget(self.msg)

        btns = QHBoxLayout()
        btns.addStretch(1)
        cancel = QPushButton("取消")
        cancel.clicked.connect(self.reject)
        btns.addWidget(cancel)
        self.ok_btn = primary_button("导入")
        self.ok_btn.clicked.connect(self._on_ok)
        btns.addWidget(self.ok_btn)
        layout.addLayout(btns)

    # ===== 校验（先本地挡一道，避免明显写错也走一轮网络）=====

    def _on_ok(self) -> None:
        parsed, error = parse_benchmark_payload(self.json_edit.toPlainText())
        if error:
            self.msg.setText(error)
            self.msg.setVisible(True)
            return
        self._parsed = parsed
        self.accept()

    def payload(self) -> Tuple[Dict[str, Any] | None, str, str]:
        """``(data, data_year, source)``；未通过校验时 data 为 ``None``。"""
        return (self._parsed, self.year_edit.text().strip(),
                self.source_edit.text().strip())


def format_benchmark_report(info: Dict[str, Any]) -> str:
    """把导入返回体里「没按你所给的值生效」的字段渲染成可读提示（无问题返回 ""）。

    三个来源（见 `core.salary_benchmark.sanitize_data` 的 `report` 出参）：
    - `unknown_keys`：字段名不认识，整条被丢弃；
    - `replaced_keys`：字段名认识但**取值非法**，已换成内置默认值；
    - `missing_keys`：字段名认识但导入数据**没给**，已用内置默认值补齐。

    任何一项被静默兜底，用户都会看到「导入成功」却数字一个都没变，然后反复重导。
    抽成纯函数是为了让判据直接测渲染，不必弹窗、不必起页面。
    """
    if not isinstance(info, dict):
        return ""
    report = info.get("data_report")
    if not isinstance(report, dict):
        # 向后兼容：旧返回体只带平铺的 unknown_keys
        legacy = info.get("unknown_keys")
        report = {"unknown_keys": legacy} if isinstance(legacy, dict) and legacy else {}
    buckets = (
        ("unknown_keys", "字段名不被识别、整条已忽略"),
        ("replaced_keys", "取值不合法、已换用内置默认值"),
        ("missing_keys", "未提供、已用内置默认值补齐"),
    )
    lines = []
    for bucket, label in buckets:
        table_map = report.get(bucket)
        if not isinstance(table_map, dict) or not table_map:
            continue
        names = "；".join(
            f"{table}: {'、'.join(str(k) for k in keys[:5])}"
            for table, keys in table_map.items() if keys)
        if names:
            lines.append(f"⚠ {label} —— {names}")
    if not lines:
        return ""
    return ("\n" + "\n".join(lines)
            + "\n请核对字段名与取值范围（学历档 / 方向 / 城市档各有一套固定 key）。")


class SalaryPage(QWidget):
    def __init__(self, main_window):
        super().__init__()
        self.mw = main_window
        self._worker = None
        #: 本页起过的全部后台 worker（销毁时统一断连，见 `_cleanup_workers`）。
        self._workers: list = []
        # 谈判会话状态
        self._negotiation_id = None
        self._messages = []
        self._coaching = []
        self._round = 0
        # 「我手里的 offer」定位块：随市场价结果卡一起出现，所以构造期是 None。
        # ⚠️ 必须在这里占位：结果区被清空时那几个控件会被 deleteLater，之后若还有
        # 人摸 self.offer_input，就是摸已析构的 C++ 对象（无 traceback 直接崩）。
        self._last_reference = None
        self.offer_input = None
        self.offer_btn = None
        self.offer_result = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.setSpacing(12)
        layout.addWidget(make_title(
            "薪资谈判",
            "先测市场价（学历 × 方向 × 城市规则计算，AI 不编数字），再和 AI 扮演的 HR 实战演练，每轮有教练点评。",
        ))

        self.tabs = QTabWidget()
        self.tabs.addTab(self._build_reference_tab(), "市场价参考")
        self.tabs.addTab(self._build_negotiation_tab(), "谈判演练")
        layout.addWidget(self.tabs, 1)

        # 换简历时立刻刷新按钮可用性（不用等下一次 showEvent）
        self.ref_resume_combo.currentIndexChanged.connect(self._sync_resume_state)

        self.overlay = BusyOverlay(self)

        # 页面销毁时断开仍在跑的后台任务回调，防止回调访问已释放的 Qt 对象。
        # 注：SalaryPage 是 QStackedWidget 的子页面，parent 关闭时不会触发它的
        # closeEvent，destroyed 才是子 widget 确定性析构钩子（与 `agent.py` 同款）。
        self.destroyed.connect(self._cleanup_workers)

    # ================= 生命周期 =================

    def _cleanup_workers(self) -> None:
        """页面销毁前断开所有后台任务回调。

        不 wait()/terminate()：worker 正在做网络请求，GUI 线程 wait 会卡住界面，
        terminate 不安全。断开信号后回调不再触发（worker 跑完自会从 `_ALIVE` 移除）；
        已经排进事件队列的那一次由各回调入口的 `isValid(self)` 守卫兜住。
        """
        for w in self._workers:
            try:
                w.success.disconnect()
                w.failed.disconnect()
            except Exception:  # noqa: BLE001 —— 测试替身可能没有 disconnect
                pass
        self._workers = []

    # ================= Tab 1：市场价参考 =================

    def _build_reference_tab(self) -> QWidget:
        host = QWidget()
        layout = QVBoxLayout(host)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(12)

        selector = card(highlight=True)
        sv = QVBoxLayout(selector)
        sv.setContentsMargins(16, 14, 16, 14)
        sv.setSpacing(8)
        sl = QHBoxLayout()
        sl.addWidget(QLabel("选择简历"))
        self.ref_resume_combo = QComboBox()
        self.ref_resume_combo.setMinimumWidth(180)
        sl.addWidget(self.ref_resume_combo, 2)
        sl.addWidget(QLabel("目标城市"))
        self.ref_city = QLineEdit(placeholderText="北京")
        self.ref_city.setFixedWidth(120)
        sl.addWidget(self.ref_city)
        sl.addWidget(QLabel("岗位提示"))
        self.ref_position = QLineEdit(placeholderText="可空")
        self.ref_position.setFixedWidth(140)
        sl.addWidget(self.ref_position)
        self.ref_btn = primary_button("测我的市场价")
        self.ref_btn.clicked.connect(self._get_reference)
        sl.addWidget(self.ref_btn)
        sv.addLayout(sl)

        # 没有简历时不要"点了才报错"：给一条引导 + 就地导入入口
        row2 = QHBoxLayout()
        self.ref_resume_hint = resume_empty_hint(
            "还没有简历 —— 点右侧「导入简历」上传一份 Word / PDF（本机解析；"
            "只读取内容估市场价，不会改动简历本身）。")
        row2.addWidget(self.ref_resume_hint, 1)
        row2.addWidget(make_import_button(self, self._on_resume_imported))
        sv.addLayout(row2)

        # 市场数据版本条：市场价是「行情表算出来的」，用户得能看到、能替换这份表。
        # 只留「导入权威数据」——原「刷新市场数据」按钮点下去必然被拒（见模块 docstring）。
        info_row = QHBoxLayout()
        info_row.setSpacing(8)
        self.bench_label = hint_label("市场数据 加载中…")
        self.bench_label.setWordWrap(False)
        info_row.addWidget(self.bench_label, 1)
        self.bench_import_btn = QPushButton("导入权威数据")
        self.bench_import_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.bench_import_btn.clicked.connect(self._open_benchmark_import)
        info_row.addWidget(self.bench_import_btn)
        sv.addLayout(info_row)

        self.bench_msg = hint_label("")
        self.bench_msg.setWordWrap(True)
        self.bench_msg.setVisible(False)
        sv.addWidget(self.bench_msg)

        layout.addWidget(selector)

        scroll_content = QWidget()
        self.ref_result_layout = QVBoxLayout(scroll_content)
        self.ref_result_layout.setContentsMargins(0, 0, 0, 0)
        self.ref_result_layout.addWidget(empty_state(
            "还没有市场价参考",
            "选择简历、填好城市后点「测我的市场价」，合理区间与建议报价会显示在这里"))
        from PySide6.QtWidgets import QScrollArea
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(scroll_content)
        layout.addWidget(scroll, 1)
        return host

    # ================= Tab 1：市场数据版本（基准表） =================

    def _load_benchmark_info(self) -> None:
        """拉当前生效的基准版本信息（只读），进页面时刷新一次。"""
        worker = LLMWorker(api_guard(db.salary_benchmark_info))
        self._workers.append(worker)
        worker.success.connect(self._on_benchmark_info)
        worker.failed.connect(lambda _msg: self._set_bench_label(None))
        worker.start()

    def _set_bench_label(self, info: Any) -> None:
        if not isinstance(info, dict) or not info:
            self.bench_label.setText("市场数据：版本信息暂不可用")
            return
        self.bench_label.setText(
            f"市场数据 v{info.get('version', 0)} · {info.get('data_year', '')} 届")

    def _set_bench_msg(self, text: str) -> None:
        self.bench_msg.setText(text)
        self.bench_msg.setVisible(bool(text))

    def _on_benchmark_info(self, info: Any) -> None:
        if not isValid(self):
            return
        self._set_bench_label(info)

    def _open_benchmark_import(self) -> None:
        dlg = _BenchmarkImportDialog(self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        data, year, source = dlg.payload()
        if not data:
            return
        self._set_bench_msg("导入中…")
        self.bench_import_btn.setEnabled(False)
        worker = LLMWorker(api_guard(
            lambda: db.salary_benchmark_import(data, year, source)))
        self._workers.append(worker)
        worker.success.connect(self._on_benchmark_imported)
        worker.failed.connect(self._on_benchmark_import_failed)
        worker.start()

    def _on_benchmark_imported(self, info: Any) -> None:
        if not isValid(self):
            return
        self.bench_import_btn.setEnabled(True)
        if not isinstance(info, dict) or not info:
            self._set_bench_msg("导入失败：没有返回版本信息")
            return
        self._set_bench_label(info)
        # 桌面版的基准数据存在**本机**（网页版那份是全局的、两端不共享），
        # 所以说成「对所有账号生效」就是错的 —— 用户会以为网页版也跟着变了。
        msg = (f"已导入到 v{info.get('version', 0)}（{info.get('data_year', '')}）"
               " —— 本机生效")
        # 字段名不认识 / 取值非法 / 缺字段都会被静默按内置基线兜底：
        # 不说清楚，用户只会反复重导（见 `format_benchmark_report`）。
        msg += format_benchmark_report(info)
        self._set_bench_msg(msg)
        self._clear_reference_result()

    def _on_benchmark_import_failed(self, msg: str) -> None:
        if not isValid(self):
            return
        self.bench_import_btn.setEnabled(True)
        self._set_bench_msg(msg)

    def _clear_reference_result(self) -> None:
        """清空结果区并回到空态。

        基准换了版本 ⇒ 之前算出的市场价已经过期，留着会误导（用户会以为那还是
        当前行情下的数字）。
        """
        while self.ref_result_layout.count():
            item = self.ref_result_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        # 定位块跟着结果卡一起被删了 —— 引用必须同步置空，否则后面再点就是
        # 访问已析构的 C++ 对象（Qt 不会抛 Python 异常，直接崩进程）。
        self._last_reference = None
        self.offer_input = None
        self.offer_btn = None
        self.offer_result = None
        self.ref_result_layout.addWidget(empty_state(
            "还没有市场价参考",
            "选择简历、填好城市后点「测我的市场价」，合理区间与建议报价会显示在这里"))

    # ================= Tab 1：测算 =================

    def _get_reference(self) -> None:
        resume_id = self.ref_resume_combo.currentData()
        if not resume_id:
            QMessageBox.warning(self, "缺少选择", "还没有简历的话，点右侧「导入简历」上传一份")
            return
        resume = db.get_resume(resume_id)
        city = self.ref_city.text().strip()
        position = self.ref_position.text().strip()

        def do():
            return db.salary_reference(resume_id, city, position)

        self.ref_btn.setEnabled(False)
        self.overlay.show_over("AI 正在读简历估市场价（数字由行情表计算）…")
        self._worker = LLMWorker(api_guard(do))
        self._workers.append(self._worker)
        self._worker.success.connect(self._on_reference)
        self._worker.failed.connect(self._on_failed)
        self._worker.start()

    def _on_reference(self, ref: dict) -> None:
        if not isValid(self):
            return
        self.overlay.hide()
        self._sync_resume_state()
        self._last_reference = ref
        while self.ref_result_layout.count():
            item = self.ref_result_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        big = card(success=True)
        bl = QVBoxLayout(big)
        bl.setContentsMargins(16, 14, 16, 14)
        title = QLabel(f"{ref.get('degree', '')} · {ref.get('direction', '')} · {ref.get('city_tier', '')}")
        title.setStyleSheet("font-size:15px; font-weight:700;")
        bl.addWidget(title)
        price = QLabel(
            f"税前月薪合理区间：{ref['salary_range']['low']:,} ~ {ref['salary_range']['high']:,} 元")
        price.setStyleSheet("font-size:22px; font-weight:800; color:#059669;")
        bl.addWidget(price)
        # ⚠️ 开口价 ≠ 目标价：数据层返回**三档** (ask, target, bottom)，开口价 = 上限 × 1.15
        # （锚定溢价）。旧版这里把 suggest_target 当「开口价」显示，而 AI 解读里又写着
        # 「第一次开口要报开口价 {ask}、不是目标价 {target}」（services/prompts.py 的
        # SALARY_ANALYSIS_SYSTEM）——展示与解读当场自相矛盾，且把 15% 锚定空间白算掉。
        ask = ref.get("suggest_ask") or ref.get("suggest_target")
        suggest = QLabel(
            f"建议开口价：{ask:,} 元    目标价：{ref['suggest_target']:,} 元"
            f"    建议底线：{ref['suggest_bottom']:,} 元")
        suggest.setStyleSheet("font-size:14px; font-weight:600; color:#6366f1;")
        bl.addWidget(suggest)
        bl.addWidget(hint_label("定薪依据：" + ref.get("market_basis", "")))
        self.ref_result_layout.addWidget(big)
        self.ref_result_layout.addWidget(self._build_offer_locator())

        if ref.get("highlights"):
            hc = card()
            hcl = QVBoxLayout(hc)
            hcl.setContentsMargins(16, 14, 16, 14)
            hcl.addWidget(section_label("简历亮点（筹码与短板）"))
            for h in ref["highlights"]:
                l = QLabel("• " + str(h))
                l.setWordWrap(True)
                hcl.addWidget(l)
            self.ref_result_layout.addWidget(hc)

        if ref.get("analysis"):
            ac = card(highlight=True)
            acl = QVBoxLayout(ac)
            acl.setContentsMargins(16, 14, 16, 14)
            acl.addWidget(section_label("AI 顾问解读"))
            analysis = QLabel(ref["analysis"])
            analysis.setWordWrap(True)
            analysis.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            acl.addWidget(analysis)
            self.ref_result_layout.addWidget(ac)

        sc = card()
        scl = QVBoxLayout(sc)
        scl.setContentsMargins(16, 14, 16, 14)
        scl.addWidget(section_label("数据来源"))
        for s in ref.get("sources", []):
            scl.addWidget(hint_label("· " + s))
        for c in ref.get("caveats", []):
            cl = hint_label("⚠ " + c)
            cl.setProperty("class", "text-warn")
            scl.addWidget(cl)
        self.ref_result_layout.addWidget(sc)

        # 一键带入谈判目标
        jump = card(warn=True)
        jl = QVBoxLayout(jump)
        jl.setContentsMargins(16, 14, 16, 14)
        jl.addWidget(hint_label("把建议开口价/底线带入「谈判演练」直接开练？"))
        btn = primary_button(
            f"带入并开始演练（开口价 {ask:,} / 底线 {ref['suggest_bottom']:,}）")
        btn.clicked.connect(lambda: self._jump_to_negotiation(ref))
        jl.addWidget(btn, 0, Qt.AlignmentFlag.AlignLeft)
        self.ref_result_layout.addWidget(jump)
        self.ref_result_layout.addStretch(1)

    def _build_offer_locator(self) -> QWidget:
        """「我手里的 offer 在市场什么位置」卡片（S1 定位的界面接线）。

        场景：用户手里已经拿到一个 offer，想知道它相对刚才算出的市场区间处在什么
        位置、还有多少可以争取。**纯本机计算**（``locate_offer`` 是纯函数）——
        不调模型、不联网，所以没有 worker、没有加载态、点一下即时出结果。

        控件在这时候才创建（结果区被清空时会连同结果卡一起被删），所以
        ``__init__`` 里只占位成 None。
        """
        loc = card(highlight=True)
        ll = QVBoxLayout(loc)
        ll.setContentsMargins(16, 14, 16, 14)
        ll.setSpacing(8)
        ll.addWidget(section_label("我手里的 offer 在市场什么位置？"))

        row = QHBoxLayout()
        row.addWidget(QLabel("offer 月薪"))
        self.offer_input = QLineEdit(placeholderText="如 18000 / 18k / 1.8万")
        self.offer_input.setFixedWidth(170)
        self.offer_input.returnPressed.connect(self._locate_offer)
        row.addWidget(self.offer_input)
        self.offer_btn = QPushButton("算定位")
        self.offer_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.offer_btn.clicked.connect(self._locate_offer)
        row.addWidget(self.offer_btn)
        row.addStretch(1)
        ll.addLayout(row)

        self.offer_result = hint_label("")
        self.offer_result.setVisible(False)
        ll.addWidget(self.offer_result)
        return loc

    def _locate_offer(self) -> None:
        """把填进来的 offer 放进市场区间定位。纯本地计算，不走 worker。"""
        ref = self._last_reference
        box = self.offer_input
        if not ref or box is None:
            return

        amount = parse_offer_amount(box.text())
        if amount is None:
            self._show_offer_position(
                None, "没读出数字 —— 填月薪就行，支持 18000 / 18k / 1.8万 这几种写法")
            return

        rng = ref.get("salary_range") or {}
        result = locate_offer(rng.get("low", 0), rng.get("high", 0), amount)
        if result.get("position") == "unknown":
            self._show_offer_position(None, "市场区间不可用，先重新算一次市场价再定位")
            return
        self._show_offer_position(result, self._offer_position_text(result))

    @staticmethod
    def _offer_position_text(result: dict) -> str:
        """把定位结果讲成人话：落在哪一档 + 还能争取多少。"""
        amount = int(result.get("offer") or 0)
        low, high = (list(result.get("range") or [0, 0]) + [0, 0])[:2]
        gap = int(result.get("gap_to_high") or 0)
        if gap > 0:
            tail = (f"还能往上争取 {gap:,} 元（到区间上限 {high:,}，"
                    f"约 {result.get('gap_to_high_pct', 0)}%）。")
        elif gap < 0:
            tail = f"已高出区间上限 {abs(gap):,} 元（上限 {high:,}）。"
        else:
            tail = "正好压在区间上限，可以再往上够一点。"
        return (f"{amount:,} 元 → {result.get('label', '')}"
                f"（区间 {low:,} ~ {high:,}）。{tail}")

    def _show_offer_position(self, result: Dict[str, Any] | None, text: str) -> None:
        """渲染定位文案并按档位换语义色（读不出数字/算不了时走警示色）。"""
        label = self.offer_result
        if label is None:
            return
        label.setText(text)
        label.setVisible(True)
        pos = (result or {}).get("position", "")
        if result is None:
            tone = "text-danger"
        elif pos in ("below_market", "low_end"):
            tone = "text-warn"
        elif pos == "above_market":
            tone = "text-success"
        else:
            tone = "hint"
        set_prop(label, "class", tone)

    def _jump_to_negotiation(self, ref: dict) -> None:
        self.tabs.setCurrentIndex(1)
        # 谈判契约（services/prompts.py 的 NEGOTIATION_COACH_SYSTEM）把传进去的第二个
        # 参数叫「开口报价」，所以这里必须填**开口价**而非目标价；填目标价等于开局就
        # 主动放弃 15% 锚定空间，第一轮就被 HR 压到底线。
        self.neg_target.setText(str(ref.get("suggest_ask") or ref.get("suggest_target")))
        self.neg_bottom.setText(str(ref["suggest_bottom"]))

    # ================= Tab 2：谈判演练 =================

    def _build_negotiation_tab(self) -> QWidget:
        host = QWidget()
        layout = QHBoxLayout(host)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(12)

        # 左：设置 + 对话
        left = QWidget()
        ll = QVBoxLayout(left)
        ll.setSpacing(12)

        setup = card(highlight=True)
        sl = QVBoxLayout(setup)
        sl.setContentsMargins(16, 14, 16, 14)
        sl.addWidget(section_label("谈判设定"))
        row1 = QHBoxLayout()
        self.neg_scenario = QComboBox()
        for key, label in [
            ("offer", "初次谈薪"), ("counter", "应对压价"),
            ("raise", "争取涨幅"), ("final", "应对「最终报价」"),
        ]:
            self.neg_scenario.addItem(label, key)
        row1.addWidget(QLabel("场景"))
        row1.addWidget(self.neg_scenario, 1)
        sl.addLayout(row1)
        row2 = QHBoxLayout()
        row2.addWidget(QLabel("开口报价"))
        self.neg_target = QLineEdit(placeholderText="如 14000（往高了报，留压价空间）")
        row2.addWidget(self.neg_target)
        row2.addWidget(QLabel("底线"))
        self.neg_bottom = QLineEdit(placeholderText="如 9000")
        row2.addWidget(self.neg_bottom)
        sl.addLayout(row2)
        row3 = QHBoxLayout()
        row3.addWidget(QLabel("业绩证明点"))
        self.neg_context = QLineEdit(placeholderText="如：独立从 0 到 1 搭建过 RAG 平台，两段大模型实习")
        row3.addWidget(self.neg_context)
        sl.addLayout(row3)
        self.neg_start_btn = primary_button("开始谈判（HR 会压价，顶住！）")
        self.neg_start_btn.clicked.connect(self._start_negotiation)
        sl.addWidget(self.neg_start_btn, 0, Qt.AlignmentFlag.AlignLeft)
        ll.addWidget(setup)

        chat_card = card()
        cl = QVBoxLayout(chat_card)
        cl.setContentsMargins(12, 10, 12, 10)
        cl.addWidget(section_label(f"谈判对话（最多 {MAX_NEGOTIATION_ROUNDS} 轮）"))
        self.neg_chat = QTextEdit()
        self.neg_chat.setReadOnly(True)
        self.neg_chat.setStyleSheet("font-size:13px;")
        self.neg_chat.setPlaceholderText(
            "点「开始谈判」后，HR 的开场话术会出现在这里，你每回应一轮就往下推进。")
        cl.addWidget(self.neg_chat, 1)
        input_row = QHBoxLayout()
        self.neg_answer = QPlainTextEdit()
        self.neg_answer.setPlaceholderText("输入你的回应…（例如：我理解预算有限，但我的项目经验能直接产出…）")
        self.neg_answer.setFixedHeight(72)
        input_row.addWidget(self.neg_answer, 1)
        self.neg_send_btn = primary_button("回应 HR")
        self.neg_send_btn.clicked.connect(self._submit_negotiation)
        self.neg_send_btn.setEnabled(False)
        input_row.addWidget(self.neg_send_btn, 0, Qt.AlignmentFlag.AlignTop)
        cl.addLayout(input_row)
        ll.addWidget(chat_card, 1)
        layout.addWidget(left, 3)

        # 右：教练点评
        right = card()
        rl = QVBoxLayout(right)
        rl.setContentsMargins(12, 10, 12, 10)
        rl.addWidget(section_label("教练逐轮点评"))
        self.coach_view = QTextEdit()
        self.coach_view.setReadOnly(True)
        self.coach_view.setPlaceholderText("每轮回应后，教练会指出你是否接受了对方的框架，并给反击话术。")
        rl.addWidget(self.coach_view, 1)
        layout.addWidget(right, 2)
        return host

    def _start_negotiation(self) -> None:
        scenario = self.neg_scenario.currentData()
        target = self.neg_target.text().strip()
        bottom = self.neg_bottom.text().strip()
        context = self.neg_context.text().strip()
        self._negotiation_id = None
        self._messages = []
        self._coaching = []
        self._round = 0
        self.neg_chat.clear()
        self.coach_view.clear()
        self.neg_send_btn.setEnabled(False)
        # 防连点：旧版开始按钮从不禁用，连点两次会起**两个**谈判会话，
        # 界面只跟最后一个 `_negotiation_id`，前一个静默留在进程内存里。
        self.neg_start_btn.setEnabled(False)

        def do():
            return db.start_negotiation(scenario, target, bottom, context)

        self.overlay.show_over("HR 正在开场…")
        self._worker = LLMWorker(api_guard(do))
        self._workers.append(self._worker)
        self._worker.success.connect(self._on_hr_opening)
        self._worker.failed.connect(self._on_failed)
        self._worker.start()

    def _on_hr_opening(self, result: dict) -> None:
        if not isValid(self):
            return
        self.overlay.hide()
        self._negotiation_id = result.get("session_id")
        opening = result.get("hr_message", "")
        self._messages.append({"role": "hr", "content": opening})
        self.neg_chat.append(f"【HR】{opening}")
        self.neg_send_btn.setEnabled(True)

    def _submit_negotiation(self) -> None:
        answer = self.neg_answer.toPlainText().strip()
        if not answer or not self._messages:
            return
        self.neg_answer.clear()
        self.neg_chat.append(f"\n【我】{answer}")
        self._messages.append({"role": "user", "content": answer})
        self.neg_send_btn.setEnabled(False)
        self.overlay.show_over("教练正在点评，HR 正在回应…")

        def do():
            return db.submit_negotiation_answer(self._negotiation_id, answer)

        self._worker = LLMWorker(api_guard(do))
        self._workers.append(self._worker)
        self._worker.success.connect(self._on_negotiation_round)
        self._worker.failed.connect(self._on_failed)
        self._worker.start()

    def _on_negotiation_round(self, result: dict) -> None:
        if not isValid(self):
            return
        self.overlay.hide()
        coaching = result.get("coaching", "")
        next_hr = result.get("next_hr_message")
        finished = result.get("is_finished", False)
        last_hr = next((m["content"] for m in reversed(self._messages) if m["role"] == "hr"), "")
        last_answer = next((m["content"] for m in reversed(self._messages) if m["role"] == "user"), "")
        self._coaching.append({"hr": last_hr, "answer": last_answer, "coaching": coaching})

        round_no = len(self._coaching)
        self.coach_view.append(f"—— 第 {round_no} 轮 ——\n{coaching}\n")

        if finished or not next_hr:
            self._finish_negotiation()
        else:
            self._messages.append({"role": "hr", "content": next_hr})
            self.neg_chat.append(f"\n【HR】{next_hr}")
            self.neg_send_btn.setEnabled(True)

    def _finish_negotiation(self) -> None:
        self.overlay.show_over("谈判结束，教练正在写总结…")
        self.neg_send_btn.setEnabled(False)

        def do():
            return db.get_negotiation_summary(self._negotiation_id)

        # ⚠️ 这里原来漏了 `start()`：worker 建好、信号也接好了，但线程根本没跑，
        # 于是「谈判结束 → 总结」永远不出现，遮罩一直挂在界面上（点不动也退不出）。
        self._worker = LLMWorker(api_guard(do))
        self._workers.append(self._worker)
        self._worker.success.connect(self._on_neg_summary)
        self._worker.failed.connect(self._on_failed)
        self._worker.start()

    def _on_neg_summary(self, result) -> None:
        if not isValid(self):
            return
        self.overlay.hide()
        summary = result.get("summary") if isinstance(result, dict) else str(result)
        self.neg_chat.append(f"\n{'=' * 40}\n【谈判结束 · 教练总结】\n{summary}")
        self.coach_view.append(f"—— 总结 ——\n{summary}")
        self.neg_start_btn.setEnabled(True)   # 一场谈完，允许再开一场

    def _on_failed(self, msg: str) -> None:
        if not isValid(self):
            return
        self.overlay.hide()
        self.neg_start_btn.setEnabled(True)   # 出错后别把开始按钮永久卡死
        self._sync_resume_state()
        QMessageBox.warning(self, "操作失败", msg)

    def showEvent(self, event):
        super().showEvent(event)
        self._refresh_resumes()
        # 市场数据版本可能已被别处刷新/导入改过，每次进页面都对一下
        self._load_benchmark_info()

    def _refresh_resumes(self, select_resume: str | None = None) -> None:
        """重建简历下拉并同步按钮状态。

        `clear()` 会丢掉当前选中，所以重建后按 id 找回来；`select_resume`
        用于「导入简历」成功后把新简历选中。
        """
        resumes = db.list_resumes()
        want = select_resume if select_resume is not None else self.ref_resume_combo.currentData()
        self.ref_resume_combo.clear()
        if not resumes:
            self.ref_resume_combo.addItem("（暂无简历）", None)
            self.ref_resume_combo.setToolTip("还没有简历：点右侧「导入简历」上传一份 Word / PDF")
        for r in resumes:
            label = (r.get("parsed_json") or {}).get("position") or r["raw_text"][:20]
            label = (label or "").replace("\n", " ").replace("\r", " ").strip()
            self.ref_resume_combo.addItem(label[:16] if label else "（未命名简历）", r["id"])
        if want:
            idx = self.ref_resume_combo.findData(want)
            if idx >= 0:
                self.ref_resume_combo.setCurrentIndex(idx)
        self._sync_resume_state()

    def _sync_resume_state(self) -> None:
        """按「有没有简历」同步引导条与主按钮：缺简历直接禁用，而不是点了才弹错。"""
        has_resume = bool(self.ref_resume_combo.currentData())
        self.ref_resume_hint.setVisible(not has_resume)
        self.ref_btn.setEnabled(has_resume)

    def _on_resume_imported(self, resume_id: str) -> None:
        """来自「导入简历」的回调：等入库成功后再刷新，并把新简历选中。"""
        self._refresh_resumes(select_resume=resume_id)