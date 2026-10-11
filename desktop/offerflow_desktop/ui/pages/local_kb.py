"""本地知识库页：看清「本地检索到底基于什么数据」，并能自查、自修。

为什么需要（计划 §9.5-4）：
    本地索引是生成时参考内容的**唯一来源**，但它对用户完全不可见 —— 用的是哪天的
    语料、到底建没建索引、是不是降级成了词袋匹配，全都无从得知；服务器语料更新了
    也无人告知，用户会一直用过期语料、且以为那就是全部。本页把状态摊开，并给出
    两个动作：**检查更新**（轻量探针）与**重建索引**。

P3b/P3c 之后新增「本地图谱与记忆」卡片（2026-09-30）：
    这是全页**唯一会让用户付费的地方**（用他自己的 Key 调模型）。所以每次点击
    都先算「要抽几份 → 约几次调用」并二次确认，界面上常驻显示累计调用次数；
    内容没变的资料按哈希跳过，误点不会重复花钱。

设计取舍：
    · 只有点「检查更新」才联网（不做后台轮询），与「离线优先」不冲突；
    · 重建在后台线程跑（首次约 20s），期间用 QTimer 轮询一个进度字典来刷 UI ——
      进度回调发生在工作线程里，**不能**直接碰控件。
"""
from __future__ import annotations

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from ...llm import LLMWorker
from ...local_ai import freshness, graph as kg, index as ix, memory as mem, pull, seed
from ..components import (
    FlowWidget,
    card,
    hint_label,
    make_title,
    primary_button,
    section_label,
    stat_card,
)
from ..local_model_dialog import LocalModelDialog

_STAGE_TEXT = {"pull": "拉取语料", "chunk": "分块", "embed": "计算向量"}
_KG_STAGE_TEXT = {"extract": "抽取实体与关系", "derive": "提炼记忆"}


def _fmt_time(ts: str) -> str:
    return str(ts)[:16].replace("T", " ") if ts else "—"


def _clear(layout) -> None:
    while layout.count():
        item = layout.takeAt(0)
        w = item.widget()
        if w is not None:
            w.deleteLater()


class LocalKnowledgeBasePage(QWidget):
    def __init__(self, main_window):
        super().__init__()
        self.mw = main_window
        self._worker = None
        self._kg_worker = None
        self._checking = False          # True = 本次结果来自联网探针
        self._progress = {"stage": "", "done": 0, "total": 0}
        self._kg_progress = {"stage": "", "done": 0, "total": 0}
        self._timer = QTimer(self)
        self._timer.setInterval(300)
        self._timer.timeout.connect(self._tick)
        self._kg_timer = QTimer(self)
        self._kg_timer.setInterval(300)
        self._kg_timer.timeout.connect(self._kg_tick)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        content = QWidget()
        layout = QVBoxLayout(content)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.setSpacing(16)
        layout.addWidget(make_title(
            "本地知识库",
            "生成简历 / 面经 / 简历优化时的参考资料，全部来自本机这份索引（离线可用）。"
            "这里可以查看它基于哪天的语料、是否需要更新，并手动重建。",
        ))

        # 用 FlowWidget 而非 QHBoxLayout：4 张统计卡横排时，窗口一窄就会把最后一张裁掉
        # （离屏实测 1000px 宽下第 4 张已溢出屏幕）。Flow 布局会自动换到下一行。
        self.stats_flow = FlowWidget(h_spacing=12, v_spacing=12)
        layout.addWidget(self.stats_flow)

        layout.addWidget(self._freshness_card())
        layout.addWidget(self._sources_card())
        layout.addWidget(self._kg_card())
        layout.addWidget(self._seed_card())
        layout.addStretch(1)

        scroll.setWidget(content)
        outer.addWidget(scroll)

    def showEvent(self, event):
        """每次显示时刷新本地状态。

        **不在构造期调 `_refresh()`**：主窗口会一次性构造全部页面，构造期做任何
        耗时/可能起线程的事，都会在页面被销毁（退出登录重建窗口）时摸到已删除的
        C++ 对象 → 进程静默崩退且无 traceback（v2.2.7 实测踩过，见
        `tests/test_ctor_async_audit.py` 的护栏）。`_refresh()` 只读本地 SQLite，
        放这里没有任何代价。
        """
        super().showEvent(event)
        self._refresh()

    # ===== 统计卡 =====
    def _fill_stats(self, local: dict) -> None:
        self.stats_flow.clear()
        idx = local.get("index") or {}
        backend = idx.get("backend") or "—"
        backend_text = "语义向量" if backend == "semantic" else (
            "词袋降级" if backend == "hash_fallback" else "未知")
        # 模型名去掉厂商前缀（BAAI/…），否则卡片被撑得很宽、与其它三张不齐
        model_short = (idx.get("model") or "").split("/")[-1] or "未记录模型"
        snap = local.get("corpus_snapshot") or {}
        cards = [
            stat_card("索引块数", str(idx.get("chunks") or 0), "#4f46e5",
                      f"覆盖 {idx.get('items') or 0} 条"),
            stat_card("向量后端", backend_text,
                      "#0ea5e9" if backend == "semantic" else "#f59e0b", model_short),
            stat_card("构建时间", _fmt_time(idx.get("built_at")), "#10b981",
                      "本地重算完成"),
            stat_card("语料快照", str(snap.get("count") or 0), "#8b5cf6",
                      f"抓取于 {_fmt_time(snap.get('fetched_at'))}"),
        ]
        # 固定宽度而非最小宽度：FlowLayout 会把带长文本的卡撑开，四张宽窄不一显得散乱；
        # 每张都给三行（title/value/sub），高度也一致。
        for c in cards:
            c.setFixedWidth(238)
        self.stats_flow.add_many(cards)

    # ===== 新鲜度卡 =====
    def _freshness_card(self) -> QFrame:
        self.fresh_frame = card()
        lay = QVBoxLayout(self.fresh_frame)
        lay.setContentsMargins(16, 14, 16, 14)
        lay.setSpacing(12)

        head = QHBoxLayout()
        head.addWidget(section_label("语料新鲜度"))
        head.addStretch(1)
        self.check_btn = primary_button("检查更新")
        self.check_btn.clicked.connect(self._check_update)
        self.rebuild_btn = primary_button("重建索引")
        self.rebuild_btn.clicked.connect(self._rebuild)
        head.addWidget(self.check_btn)
        head.addWidget(self.rebuild_btn)
        lay.addLayout(head)

        self.fresh_label = QLabel("")
        self.fresh_label.setWordWrap(True)
        lay.addWidget(self.fresh_label)

        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setTextVisible(True)
        self.progress.hide()
        lay.addWidget(self.progress)
        return self.fresh_frame

    def _set_fresh_style(self, cls: str) -> None:
        self.fresh_frame.setProperty("class", cls)
        self.fresh_frame.style().unpolish(self.fresh_frame)
        self.fresh_frame.style().polish(self.fresh_frame)

    # ===== 数据源卡 =====
    def _sources_card(self) -> QFrame:
        frame = card()
        lay = QVBoxLayout(frame)
        lay.setContentsMargins(16, 14, 16, 14)
        lay.setSpacing(8)
        lay.addWidget(section_label("数据来源（本地快照）"))
        self.sources_box = QVBoxLayout()
        self.sources_box.setSpacing(4)
        lay.addLayout(self.sources_box)
        lay.addWidget(hint_label(
            "索引由这四路快照在本地重新计算得到。共享语料来自服务器全站语料库；"
            "简历与岗位是你自己的数据；薪资基准是规则表、不进向量库。"))
        return frame

    def _fill_sources(self, local: dict) -> None:
        _clear(self.sources_box)
        for s in local.get("sources") or []:
            row = QLabel(
                f"{s.get('label') or s.get('source')}："
                f"{(s.get('count') or 0) if s.get('cached') else '未缓存'} 条"
                f"　·　抓取于 {_fmt_time(s.get('fetched_at'))}"
            )
            row.setProperty("class", "hint")
            self.sources_box.addWidget(row)

    # ===== 本地图谱与记忆（P3b / P3c） =====
    #
    # 这一块是**唯一会产生模型费用的地方**（用户自己的 Key）。所以：
    #   · 点按钮前先算「要抽几份 → 约几次调用」，并让用户确认；
    #   · 内容没变过的资料不会重复抽（按内容哈希判重），重复点不会白花钱；
    #   · 界面上把「累计调用次数」摊开，用户随时知道花了多少次。
    def _kg_card(self) -> QFrame:
        frame = card()
        lay = QVBoxLayout(frame)
        lay.setContentsMargins(16, 14, 16, 14)
        lay.setSpacing(12)

        head = QHBoxLayout()
        head.addWidget(section_label("本地图谱与记忆"))
        head.addStretch(1)
        # 本机模型入口：本地 AI 直连模型，用的是本机这份配置，与云端账号那份
        # （「模型设置」页）不是同一处 —— 所以入口就放在这儿，缺什么就地能补。
        self.kg_model_btn = QPushButton("本地模型…")
        self.kg_model_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.kg_model_btn.clicked.connect(self._open_local_model)
        head.addWidget(self.kg_model_btn)
        self.kg_build_btn = primary_button("抽取图谱")
        self.kg_build_btn.clicked.connect(self._kg_build)
        self.kg_derive_btn = primary_button("提炼记忆")
        self.kg_derive_btn.clicked.connect(self._kg_derive)
        self.kg_show_btn = QPushButton("查看内容")
        self.kg_show_btn.clicked.connect(self._kg_show)
        head.addWidget(self.kg_build_btn)
        head.addWidget(self.kg_derive_btn)
        head.addWidget(self.kg_show_btn)
        lay.addLayout(head)

        self.kg_label = QLabel("")
        self.kg_label.setWordWrap(True)
        lay.addWidget(self.kg_label)

        self.kg_recent = QLabel("")
        self.kg_recent.setWordWrap(True)
        self.kg_recent.setProperty("class", "hint")
        lay.addWidget(self.kg_recent)

        self.kg_progress = QProgressBar()
        self.kg_progress.setRange(0, 100)
        self.kg_progress.setTextVisible(True)
        self.kg_progress.hide()
        lay.addWidget(self.kg_progress)

        foot = QHBoxLayout()
        foot.addWidget(hint_label(
            "只对你自己的资料（简历 / 岗位 / 面经）建图，不含共享语料。"
            "抽取时每份资料调用 1 次模型、记忆提炼整批只调用 1 次；"
            "内容没变过的资料不会重复抽取。"))
        foot.addStretch(1)
        self.kg_clear_btn = QPushButton("清空图谱")
        self.kg_clear_btn.clicked.connect(self._kg_clear_graph)
        self.kg_clear_mem_btn = QPushButton("清空记忆")
        self.kg_clear_mem_btn.clicked.connect(self._kg_clear_memory)
        foot.addWidget(self.kg_clear_btn)
        foot.addWidget(self.kg_clear_mem_btn)
        lay.addLayout(foot)
        return frame

    def _fill_kg(self) -> None:
        try:
            g = kg.stats()
            m = mem.stats()
            av = kg.available()
        except Exception as exc:  # noqa: BLE001
            self.kg_label.setText(f"读取图谱状态失败：{type(exc).__name__}: {exc}")
            return

        if av.get("ok"):
            src = "本机配置" if av.get("source") == "local_model" else "账号同步"
            head = f"模型：{av.get('model') or '已配置'}　·　{src}"
        else:
            head = (f"⚠ {av.get('reason')}"
                    "　→ 点右上角「本地模型…」即可配置")
        if g.get("exists"):
            gtxt = (f"图谱：{g.get('docs')} 份资料 / {g.get('entities')} 实体 / "
                    f"{g.get('relations')} 关系　·　抽取于 {_fmt_time(g.get('built_at'))}")
        else:
            gtxt = "图谱：尚未抽取"
        mtxt = f"记忆：{m.get('count') or 0} 条" if m.get("exists") else "记忆：暂无"
        calls = g.get("calls") or 0
        ctxt = f"累计抽取调用 {calls} 次" if calls else ""
        self.kg_label.setText("\n".join(x for x in (head, f"{gtxt}　·　{mtxt}　·　{ctxt}") if x))

        try:
            rows = mem.recent(5)
        except Exception:  # noqa: BLE001
            rows = []
        if rows:
            self.kg_recent.setText("最近记忆：\n" + "\n".join(
                f"· {r.get('text')}" for r in rows))
        else:
            self.kg_recent.setText("")

    def _open_local_model(self) -> None:
        """配置「本机模型」—— 本地 AI 直连用的那一份（可填本机 Ollama）。

        与「模型设置」页的区别：那个存的是**云端账号**配置、只回掩码，
        本机拿不到明文 Key，无法直连；这里的配置只落在本机。
        """
        dlg = LocalModelDialog(self)
        dlg.exec()
        self._fill_kg()

    def _kg_busy(self, busy: bool) -> None:
        for b in (self.kg_build_btn, self.kg_derive_btn, self.kg_show_btn,
                  self.kg_clear_btn, self.kg_clear_mem_btn, self.kg_model_btn):
            b.setEnabled(not busy)

    def _kg_on_progress(self, stage: str, done: int, total: int) -> None:
        """工作线程回调：只写 dict（主线程由 QTimer 读），不碰控件。"""
        self._kg_progress = {"stage": stage, "done": done, "total": total}

    def _kg_tick(self) -> None:
        p = self._kg_progress
        total = int(p.get("total") or 0)
        done = int(p.get("done") or 0)
        stage = _KG_STAGE_TEXT.get(p.get("stage") or "", "处理中")
        if total > 0:
            self.kg_progress.setValue(max(0, min(100, int(done * 100 / total))))
            self.kg_progress.setFormat(f"{stage} {done}/{total}")
        else:
            self.kg_progress.setFormat(f"{stage} …")

    def _kg_start(self, fn, on_ok) -> None:
        self._kg_busy(True)
        self._kg_progress = {"stage": "", "done": 0, "total": 0}
        self.kg_progress.setValue(0)
        self.kg_progress.setFormat("准备中 …")
        self.kg_progress.show()
        self._kg_timer.start()
        worker = LLMWorker(fn)
        worker.success.connect(on_ok)
        worker.failed.connect(self._kg_fail)
        worker.start()
        self._kg_worker = worker

    def _kg_stop(self) -> None:
        self._kg_worker = None
        self._kg_timer.stop()
        self.kg_progress.hide()
        self._kg_busy(False)

    def _kg_fail(self, msg: str) -> None:
        self._kg_stop()
        self._set_fresh_style("card-danger")
        self.kg_label.setText(f"操作失败：{msg}")

    def _kg_build(self) -> None:
        if self._kg_worker is not None:
            return
        av = kg.available()
        if not av.get("ok"):
            QMessageBox.information(self, "抽取图谱", av.get("reason") or "模型未配置")
            return
        try:
            plan = kg.plan()
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, "抽取图谱", f"读取资料失败：{exc}")
            return
        if not plan.get("total"):
            QMessageBox.information(
                self, "抽取图谱",
                "没有可建图的资料。需要至少一份你自己的简历 / 岗位 / 面经"
                f"（正文不少于 {kg.MIN_DOC_TEXT} 字），先同步或新建一份再试。")
            return
        if not plan.get("pending"):
            QMessageBox.information(
                self, "抽取图谱",
                f"{plan['total']} 份资料都已抽取过、内容没有变化，无需重复调用模型。")
            return

        sample = "\n".join(f"· {d['title']}" for d in plan["docs"][:6])
        more = "" if plan["pending"] <= 6 else f"\n… 等共 {plan['pending']} 份"
        ans = QMessageBox.question(
            self, "抽取图谱",
            f"本次将抽取 {plan['pending']} 份资料 → **约 {plan['pending']} 次模型调用**\n"
            f"（按你配置的模型 {av.get('model') or ''} 计费；已抽取过且内容未变的 "
            f"{plan.get('unchanged') or 0} 份会跳过）\n\n{sample}{more}\n\n确定继续吗？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if ans != QMessageBox.StandardButton.Yes:
            return
        self.kg_label.setText(f"正在抽取 {plan['pending']} 份资料 …")
        self._kg_start(lambda: kg.build(progress=self._kg_on_progress), self._kg_build_ok)

    def _kg_build_ok(self, report) -> None:
        self._kg_stop()
        if report.get("error") and not report.get("extracted"):
            self._set_fresh_style("card-danger")
            self.kg_label.setText(f"抽取未能进行：{report['error']}")
            self._fill_kg()
            return
        parts = [f"抽取完成：新抽 {report.get('extracted')} 份 / 跳过 {report.get('skipped')} 份"
                 f" / 调用 {report.get('calls')} 次"
                 f"（实体 {report.get('entities')} 关系 {report.get('relations')}）"]
        if report.get("failed"):
            parts.append(f"失败 {report['failed']} 份")
        if report.get("truncated"):
            parts.append("达到本次上限，剩余资料可再次点击继续")
        if report.get("aborted"):
            parts.append(report["aborted"])
        self._set_fresh_style("card-warn" if (report.get("failed") or report.get("aborted"))
                              else "card-success")
        self.kg_label.setText("\n".join(parts))
        self._refresh()

    def _kg_derive(self) -> None:
        if self._kg_worker is not None:
            return
        av = mem.available()
        if not av.get("ok"):
            QMessageBox.information(self, "提炼记忆", av.get("reason") or "模型未配置")
            return
        st = mem.stats()
        ans = QMessageBox.question(
            self, "提炼记忆",
            "将从你的简历 / 岗位 / 面经中提炼「关于你本人的稳定事实」，"
            "**本次只调用 1 次模型**（整批资料合并成一次请求）。\n\n"
            f"当前已有记忆 {st.get('count') or 0} 条，重复的事实会自动跳过。\n\n确定继续吗？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if ans != QMessageBox.StandardButton.Yes:
            return
        self.kg_label.setText("正在提炼记忆 …")
        self._kg_start(lambda: mem.derive(progress=self._kg_on_progress), self._kg_derive_ok)

    def _kg_derive_ok(self, report) -> None:
        self._kg_stop()
        if not report.get("ok"):
            self._set_fresh_style("card-danger")
            self.kg_label.setText(f"提炼未能进行：{report.get('error')}")
            self._fill_kg()
            return
        self._set_fresh_style("card-success")
        self.kg_label.setText(
            f"记忆提炼完成：新增 {report.get('added')} 条 / 跳过重复 {report.get('skipped')} 条"
            f" / 调用 {report.get('calls')} 次")
        self._refresh()

    def _kg_show(self) -> None:
        try:
            rows = kg.sample_relations(20)
            g = kg.stats()
            mems = mem.recent(10)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, "查看内容", f"读取失败：{exc}")
            return
        if not rows and not mems:
            QMessageBox.information(self, "查看内容", "图谱与记忆都还是空的，先点「抽取图谱」。")
            return
        blocks = []
        if rows:
            blocks.append(f"图谱（{g.get('entities')} 实体 / {g.get('relations')} 关系）：\n" +
                          "\n".join(f"· {r['head']} {r['rel']} {r['tail']}（{r['title']}）"
                                    for r in rows))
        if mems:
            blocks.append("记忆：\n" + "\n".join(f"· [{m.get('item_type')}] {m.get('text')}"
                                              for m in mems))
        box = QMessageBox(self)
        box.setWindowTitle("本地图谱与记忆")
        box.setText("\n\n".join(blocks)[:3000])
        box.exec()

    def _kg_clear_graph(self) -> None:
        ans = QMessageBox.question(
            self, "清空图谱",
            "将删除本机已抽取的全部实体与关系（不影响记忆、不影响向量索引）。\n"
            "以后再抽取需要重新花模型调用。\n\n确定清空吗？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if ans != QMessageBox.StandardButton.Yes:
            return
        res = kg.reset()
        self.kg_label.setText("已清空图谱。" if res.get("ok") else f"清空失败：{res.get('error')}")
        self._refresh()

    def _kg_clear_memory(self) -> None:
        ans = QMessageBox.question(
            self, "清空记忆",
            "将删除本机全部记忆条目（不影响图谱、不影响向量索引）。\n\n确定清空吗？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if ans != QMessageBox.StandardButton.Yes:
            return
        res = mem.clear()
        self.kg_label.setText("已清空记忆。" if res.get("ok") else f"清空失败：{res.get('error')}")
        self._refresh()

    # ===== 种子卡 =====
    def _seed_card(self) -> QFrame:
        frame = card()
        lay = QVBoxLayout(frame)
        lay.setContentsMargins(16, 14, 16, 14)
        lay.setSpacing(8)
        head = QHBoxLayout()
        head.addWidget(section_label("开箱语料（随包种子）"))
        head.addStretch(1)
        btn = QPushButton("恢复随包语料")
        btn.clicked.connect(self._restore_seed)
        head.addWidget(btn)
        lay.addLayout(head)
        self.seed_label = QLabel("")
        self.seed_label.setWordWrap(True)
        lay.addWidget(self.seed_label)
        lay.addWidget(hint_label(
            "安装包内自带一份只含「共享语料」的初始索引，让首次启动（即使没网）也能立刻"
            "用上本地检索。它不包含你的简历 / 岗位。"))
        return frame

    def _fill_seed(self) -> None:
        root = seed.seed_root()
        if root:
            self.seed_label.setText(f"已随包提供种子语料：{root}"
                                    "（仅在你还没有索引时铺设，不会覆盖你的数据）")
        else:
            self.seed_label.setText(
                "未检测到随包种子语料（源码运行属正常）。此情况下首次需联网建立索引。")

    # ===== 刷新（不联网） =====
    def _refresh(self) -> None:
        try:
            local = freshness.local_state()
        except Exception as exc:  # noqa: BLE001
            self.fresh_label.setText(f"读取本地索引状态失败：{type(exc).__name__}: {exc}")
            return
        self._fill_stats(local)
        self._fill_sources(local)
        self._fill_seed()
        self._fill_kg()
        if not self._checking:
            self._set_fresh_style("card")
            idx = local.get("index") or {}
            if not idx.get("exists"):
                self.fresh_label.setText("本地还没有索引 —— 点「重建索引」建立（需联网拉取一次）。")
            else:
                self.fresh_label.setText(
                    "点「检查更新」可与服务器语料条数比对，确认本地是否需要重建。")

    # ===== 检查更新（联网） =====
    def _check_update(self) -> None:
        if self._worker is not None:
            return
        self.check_btn.setEnabled(False)
        self.fresh_label.setText("正在检查服务器语料 …")
        worker = LLMWorker(lambda: freshness.check(timeout=15, remote=True))
        worker.success.connect(self._on_check_ok)
        worker.failed.connect(self._on_check_fail)
        worker.start()
        self._worker = worker

    def _on_check_ok(self, info) -> None:
        self._worker = None
        self.check_btn.setEnabled(True)
        self._checking = True
        local = info.get("local") or {}
        self._fill_stats(local)
        remote = info.get("remote") or {}
        parts = []
        if remote.get("ok"):
            parts.append(f"服务器语料共 {remote.get('total')} 条"
                         f"（简历 {remote['by_type'].get('resume', 0)} / "
                         f"JD {remote['by_type'].get('jd', 0)} / "
                         f"面经 {remote['by_type'].get('interview', 0)}）")
        else:
            parts.append(f"未能读取服务器语料（{remote.get('error')}）")
        if info.get("stale"):
            self._set_fresh_style("card-warn")
            parts.append("建议重建索引：" + "；".join(info.get("reasons") or []))
        else:
            self._set_fresh_style("card-success")
            parts.append("本地索引与服务器一致，无需重建。")
        self.fresh_label.setText("\n".join(parts))

    def _on_check_fail(self, msg: str) -> None:
        self._worker = None
        self.check_btn.setEnabled(True)
        self._set_fresh_style("card-danger")
        self.fresh_label.setText(f"检查失败：{msg}")

    # ===== 重建索引（联网 + 重算，后台线程） =====
    def _rebuild(self) -> None:
        if self._worker is not None:
            return
        self.check_btn.setEnabled(False)
        self.rebuild_btn.setEnabled(False)
        self._progress = {"stage": "", "done": 0, "total": 0}
        self.progress.setValue(0)
        self.progress.setFormat("准备中 …")
        self.progress.show()
        self._timer.start()
        self.fresh_label.setText("正在重建索引（首次约 20 秒；离线时使用本地快照重建）…")
        worker = LLMWorker(lambda: ix.rebuild(progress=self._on_progress))
        worker.success.connect(self._on_rebuild_ok)
        worker.failed.connect(self._on_rebuild_fail)
        worker.start()
        self._worker = worker

    def _on_progress(self, stage: str, done: int, total: int) -> None:
        """工作线程回调：只写普通 dict（主线程由 QTimer 读取），不碰控件。"""
        self._progress = {"stage": stage, "done": done, "total": total}

    def _tick(self) -> None:
        p = self._progress
        total = int(p.get("total") or 0)
        done = int(p.get("done") or 0)
        stage = _STAGE_TEXT.get(p.get("stage") or "", "处理中")
        if total > 0:
            pct = int(done * 100 / total)
            self.progress.setValue(max(0, min(100, pct)))
            self.progress.setFormat(f"{stage} {done}/{total}")
        else:
            self.progress.setFormat(f"{stage} …")

    def _stop_busy(self) -> None:
        self._timer.stop()
        self.progress.hide()
        self.check_btn.setEnabled(True)
        self.rebuild_btn.setEnabled(True)

    def _on_rebuild_ok(self, report) -> None:
        self._worker = None
        self._stop_busy()
        self._checking = False
        pull_info = report.get("pull") or {}
        failed = [k for k, v in pull_info.items() if isinstance(v, dict) and not v.get("ok")]
        self._set_fresh_style("card-success" if not failed else "card-warn")
        text = (f"重建完成：{report.get('chunks')} 块 / {report.get('embedded')} 向量 / "
                f"耗时 {int((report.get('elapsed_ms') or 0) / 1000)} 秒 / "
                f"后端 {report.get('backend')}")
        if failed:
            text += f"\n以下数据源未拉到，已用本地旧快照参与重建：{'、'.join(failed)}"
        self.fresh_label.setText(text)
        self._refresh()

    def _on_rebuild_fail(self, msg: str) -> None:
        self._worker = None
        self._stop_busy()
        self._set_fresh_style("card-danger")
        self.fresh_label.setText(f"重建失败：{msg}")

    # ===== 恢复随包语料 =====
    def _restore_seed(self) -> None:
        info = seed.ensure_seeded(force=False)
        if not info.get("seeded"):
            reason = info.get("reason")
            if reason == "already_exists":
                ans = QMessageBox.question(
                    self, "恢复随包语料",
                    "这会用安装包内的初始语料索引覆盖当前本地索引。\n"
                    "你自己的简历 / 岗位数据不会丢失，重建索引即可重新纳入。\n\n确定继续吗？",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                )
                if ans != QMessageBox.StandardButton.Yes:
                    return
                info = seed.ensure_seeded(force=True)
            if not info.get("seeded"):
                detail = info.get("detail") or info.get("seed_model") or reason
                self.fresh_label.setText(
                    f"未能恢复随包语料：{reason}（{detail}）。"
                    "源码运行或未生成种子时属正常，直接点「重建索引」即可。")
                return
        self._checking = False
        self.fresh_label.setText(f"已恢复随包初始语料（{info.get('files')}）。")
        self._refresh()
