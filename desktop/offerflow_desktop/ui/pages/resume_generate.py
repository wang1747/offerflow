"""简历生成页：结构化表单 → AI 生成 → 评分/保真 → 逐段候选 → 入库。"""

import json
import re
from datetime import datetime

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QComboBox, QDialog, QFileDialog, QFrame, QHBoxLayout, QLabel, QLineEdit,
    QMessageBox, QPlainTextEdit, QPushButton, QScrollArea, QSplitter,
    QTabWidget, QVBoxLayout, QWidget, QGridLayout, QInputDialog,
)

from ... import api_client as db
from ... import database as local_db      # 导出目录等本机配置仍存本地 SQLite
from ...config import MIN_RESUME_LENGTH
from ...core import json_resume, pdf_verify
from ...core.sections import BODY_SECTIONS, section_labels_in_order
from ...core.undo import DEFAULT_LIMIT as UNDO_LIMIT, UndoStack
from ...llm import LLMWorker
from ...services import resume_generation as rg
from ...services import templates
from ...services.exporter import (
    default_export_path, estimate_layout, export_docx, export_txt,
)
from ...services.pdf_exporter import export_pdf
from ..components import (
    BusyOverlay, FlowWidget, card, confirm_placeholders, danger_button,
    empty_state, flow_tags, hint_label, make_title, primary_button,
    retrieval_summary, section_label, tag_label,
)

# 导入对话框与文本提取已抽到 ui/import_resume.py，供面试准备 / 薪资 / 岗位匹配复用
from ..export_result import notify_export_result
from ..import_resume import ImportResumeDialog, extract_resume_text
from ..template_picker import (
    TemplatePickerDialog, load_export_options, save_export_options,
)


def _period_key(item: dict) -> tuple:
    """把 start/end 时间转成可比较的元组，用于「按时间倒序」排序。"""
    text = str(item.get("start") or item.get("end") or "")
    nums = re.findall(r"\d+", text)
    if not nums:
        return (0, 0)
    year = int(nums[0])
    month = int(nums[1]) if len(nums) > 1 else 1
    return (year, month)


# ===== 表单快照 / 撤销 / 草稿 的常量 =====
# 表单区容器的 objectName：快照靠它锁定范围，不靠属性名（见 _keyed_widgets）
FORM_HOST_NAME = "resumeGenerateFormHost"
# 草稿存本地库的 scope。**不带账号名** —— 本地库本身就是按账号分文件的
# （database._user_db_path），所以换了账号读到的自然是另一份。
DRAFT_SCOPE = "resume_generate"
# 输入停顿多久算"落定"。太短会把一次连续输入切成一堆快照，把 200 步吃光。
SNAPSHOT_DEBOUNCE_MS = 900


def _friendly_time(text: str) -> str:
    """把 ISO 时间串说成人话（草稿提示里要用）。失败就原样返回。"""
    try:
        dt = datetime.fromisoformat(str(text))
    except (TypeError, ValueError):
        return "上次"
    delta = datetime.now() - dt
    if delta.total_seconds() < 0:
        return "刚刚"
    if delta.total_seconds() < 60:
        return "刚刚"
    if delta.total_seconds() < 3600:
        return f"{int(delta.total_seconds() // 60)} 分钟前"
    if delta.total_seconds() < 86400:
        return f"{int(delta.total_seconds() // 3600)} 小时前"
    return dt.strftime("%m-%d %H:%M")


# ===== 生成历史（判据见 tests/test_resume_history.py）=====

def _generation_of(item: dict) -> dict:
    """取 `parsed_json.generation`（生成结果本体）。取不到给空 dict，绝不抛。"""
    pj = item.get("parsed_json")
    if not isinstance(pj, dict):
        return {}
    g = pj.get("generation")
    return g if isinstance(g, dict) else {}


def _history_label(item: dict) -> str:
    """历史条目的显示名：`岗位 · vN`（对齐网页版 `{position || name} · v{version}`）。"""
    g = _generation_of(item)
    name = str(g.get("position") or g.get("name") or "").strip() or "未命名"
    ver = item.get("version") or g.get("version")
    return f"{name} · v{ver}" if ver else name


def _generated_history(items) -> list:
    """只留「AI 生成」的记录并按版本倒序。

    过滤条件与网页版逐字一致：`parsed_json.kind === "generated"` **且**
    `parsed_json.generation` 存在。不能只看 `parsed_json` 里有没有
    `structured`：优化流程产出的记录也有 structured，混进来会把
    「生成历史」变成「所有简历」。
    """
    out = []
    for it in (items or []):
        if not isinstance(it, dict):
            continue
        pj = it.get("parsed_json")
        if not isinstance(pj, dict):
            continue
        if pj.get("kind") != "generated" or not isinstance(pj.get("generation"), dict):
            continue
        out.append(it)
    out.sort(key=lambda x: int(x.get("version") or 0), reverse=True)
    return out


def _result_from_history(item: dict) -> dict:
    """把一条历史记录还原成 `_render_result` 认得的 result 结构。

    服务端把 `generation` 存成「扁平的一份」（name/position/ats/quality/…），
    而 `_render_result` 读的是 `result["structured"]` + `result["ats"]` 这套
    桌面版形状 —— 这里做一次翻译，缺 `structured` 时用 generation 的扁平字段补。
    """
    pj = item.get("parsed_json") if isinstance(item.get("parsed_json"), dict) else {}
    g = _generation_of(item)
    structured = pj.get("structured")
    if not isinstance(structured, dict):
        structured = {k: g.get(k) for k in (
            "name", "position", "contact", "summary", "education",
            "experiences", "skills", "certifications")}
    return {
        "id": g.get("id") or item.get("id"),
        "version": item.get("version") or g.get("version"),
        "document_id": g.get("document_id"),
        "structured": structured,
        "resume_text": g.get("resume_text") or item.get("raw_text") or "",
        "tips": g.get("tips") or [],
        "ats": g.get("ats") or {},
        "gap": g.get("gap") or {},
        "fidelity": g.get("fidelity") or {},
        # 写入侧用的是 ``review`` 键（``services.resume_generation`` 与
        # ``guard_result`` 都写它）；早期只读 ``quality`` ⇒ 本次会话刚生成的记录，
        # 点开预览时「内容质量」整块卡片不显示（只有从服务端重拉的记录才正常）。
        # 生成本地化之后两者都可能存在（``review`` = 桌面规则引擎报告、
        # ``quality`` = 服务端同口径的 7 维度报告），取 UI 能渲染的那份：先 ``review``。
        "review": g.get("review") or g.get("quality") or {},
        "sufficiency": g.get("sufficiency") or {},
    }


def _edu_filled(data: dict) -> bool:
    """这条教育经历卡片是否填了实质内容。

    ``degree`` 有默认值（下拉第一项），**不能**算作已填 —— 否则构造期预加的
    空卡片看起来永远"有内容"，`_generate` 里的必填校验就恒为假。
    """
    return any(str(data.get(k) or "").strip()
               for k in ("school", "major", "start", "end", "detail"))


def _exp_filled(data: dict) -> bool:
    """这条经历卡片是否填了实质内容（名称 / 角色 / 要点任一项即可）。"""
    return bool(str(data.get("name") or "").strip()
                or str(data.get("role") or "").strip()
                or (data.get("bullets") or []))


class EduCard(QFrame):
    """一条教育经历的编辑卡片。"""

    def __init__(self, on_delete):
        super().__init__()
        self.setProperty("class", "card")
        layout = QGridLayout(self)
        layout.setContentsMargins(12, 10, 12, 10)
        self.school = QLineEdit(placeholderText="学校")
        self.major = QLineEdit(placeholderText="专业")
        self.degree = QComboBox()
        for d in ("本科", "硕士", "博士", "大专"):
            self.degree.addItem(d)
        self.start = QLineEdit(placeholderText="2023-09")
        self.end = QLineEdit(placeholderText="2027-06")
        self.detail = QLineEdit(placeholderText="主修课程 / 绩点 / 荣誉（可空）")
        layout.addWidget(self.school, 0, 0)
        layout.addWidget(self.major, 0, 1)
        layout.addWidget(self.degree, 0, 2)
        layout.addWidget(self.start, 0, 3)
        layout.addWidget(self.end, 0, 4)
        del_btn = danger_button("删除")
        del_btn.clicked.connect(lambda: on_delete(self))
        layout.addWidget(del_btn, 0, 5)
        layout.addWidget(self.detail, 1, 0, 1, 6)

    def data(self):
        return {
            "school": self.school.text().strip(),
            "major": self.major.text().strip(),
            "degree": self.degree.currentText(),
            "start": self.start.text().strip(),
            "end": self.end.text().strip(),
            "detail": self.detail.text().strip(),
        }


class ExpCard(QFrame):
    """一段经历的编辑卡片（项目/实习/工作）。"""

    def __init__(self, on_delete, bullet_hint="动词 + 对象 + 方法 + 量化成果"):
        super().__init__()
        self.setProperty("class", "card")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 10)
        top = QHBoxLayout()
        self.type_combo = QComboBox()
        for t, label in (("project", "项目"), ("internship", "实习"), ("work", "工作")):
            self.type_combo.addItem(label, t)
        self.name = QLineEdit(placeholderText="项目 / 公司名称")
        self.role = QLineEdit(placeholderText="角色")
        self.start = QLineEdit(placeholderText="2025-03")
        self.end = QLineEdit(placeholderText="至今")
        del_btn = danger_button("删除")
        del_btn.clicked.connect(lambda: on_delete(self))
        for w in (self.type_combo, self.name, self.role, self.start, self.end, del_btn):
            top.addWidget(w)
        layout.addLayout(top)
        self.bullets = QPlainTextEdit()
        self.bullets.setPlaceholderText(
            f"每行一条要点（3~5 条）\n提示：{bullet_hint}\n例：主导 XX 系统后端开发，基于 Python + FastAPI 支撑日均 10 万请求"
        )
        self.bullets.setFixedHeight(110)
        layout.addWidget(self.bullets)

    def data(self):
        return {
            "type": self.type_combo.currentData(),
            "name": self.name.text().strip(),
            "role": self.role.text().strip(),
            "start": self.start.text().strip(),
            "end": self.end.text().strip(),
            "bullets": [l.strip() for l in self.bullets.toPlainText().splitlines() if l.strip()],
        }


class CandidatesDialog(QDialog):
    """展示 3 个改写候选供选择。"""

    def __init__(self, section_title: str, candidates, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"候选重写 —— {section_title}")
        self.resize(640, 520)
        self.chosen = None
        layout = QVBoxLayout(self)
        layout.addWidget(hint_label("选择一个候选，应用后会重新渲染并重新评分："))
        for i, cand in enumerate(candidates):
            frame = card()
            f_layout = QVBoxLayout(frame)
            f_layout.setContentsMargins(12, 10, 12, 10)
            if isinstance(cand, str):
                text = cand
            else:
                text = json.dumps(cand, ensure_ascii=False, indent=2)
            preview = QLabel(text)
            preview.setWordWrap(True)
            preview.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            btn = primary_button(f"采用候选 {i + 1}")
            btn.clicked.connect(lambda _=False, c=cand: self._choose(c))
            f_layout.addWidget(preview)
            f_layout.addWidget(btn, 0, Qt.AlignmentFlag.AlignLeft)
            layout.addWidget(frame)
        cancel = QPushButton("都不采用")
        cancel.clicked.connect(self.reject)
        layout.addWidget(cancel)

    def _choose(self, cand):
        self.chosen = cand
        self.accept()


class ResumeGeneratePage(QWidget):
    def __init__(self, main_window):
        super().__init__()
        self.mw = main_window
        self._worker = None
        self._current_result = None
        # 发起生成时那一刻的 JD 文本与表单快照：守门层要拿**发起时**的输入核对
        # 「生成结果里的数字有没有出处」。用"完成时刻"输入框里的内容会与实际
        # 送进模型的输入不一致（生成期间用户改了 JD 却算在结果头上）。
        self._last_jd = ""
        self._template_key = None          # 上次选过的版式，导出时记住
        # 外观与分区顺序是**独立于版式**的两项设置：从本机配置读回来，
        # 所以换版式不会把它们清掉（这正是把两条轴拆开的意义）。
        self._appearance, self._order = load_export_options()
        self._edu_cards = []
        self._exp_cards = []
        self._skills = []
        # 表单快照 / 撤销 / 草稿（见下方「表单快照」一节）
        self._undo = UndoStack(UNDO_LIMIT)
        self._restoring = False             # 回填期间不要再触发快照
        self._draft_checked = False         # 草稿只问一次，别每次切页都弹
        self._snap_timer = QTimer(self)
        self._snap_timer.setSingleShot(True)
        self._snap_timer.setInterval(SNAPSHOT_DEBOUNCE_MS)
        self._snap_timer.timeout.connect(self._on_form_settled)

        # 生成历史 / 版本链（对齐网页版 ResumeGenerate 的「生成历史」卡片）
        self._history: list = []
        self._versions: list = []
        self._history_loaded = False
        self._history_worker = None
        self._versions_worker = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.setSpacing(12)
        layout.addWidget(make_title(
            "简历生成",
            "填写真实信息，AI 优化表达生成一页内中文简历（约 800 字）。事实保真：绝不编造你没写的技能和数字。",
        ))

        # 已有现成简历的入口
        import_bar = card(highlight=True)
        ib = QHBoxLayout(import_bar)
        ib.setContentsMargins(16, 14, 16, 14)
        ib.addWidget(hint_label("已有现成简历？粘贴导入即可，无需重新生成。"))
        self.import_btn = primary_button("导入已有简历")
        self.import_btn.clicked.connect(self._import_resume)
        ib.addStretch(1)
        ib.addWidget(self.import_btn)
        layout.addWidget(import_bar)

        splitter = QSplitter(Qt.Orientation.Horizontal)

        # ===== 左侧表单 =====
        form_host = QWidget()
        # 给表单区一个名字：快照/草稿靠它把范围锁定在"用户填的输入框"上，
        # 不靠属性名（本页 self.start / self.end 被教育卡与经历卡各用了一次，
        # 按属性名快照会漏掉其中一半）。
        form_host.setObjectName(FORM_HOST_NAME)
        form_layout = QVBoxLayout(form_host)
        form_layout.setContentsMargins(0, 0, 8, 0)

        dir_card = card()
        dir_layout = QVBoxLayout(dir_card)
        dir_layout.setContentsMargins(16, 14, 16, 14)
        dir_layout.addWidget(section_label("求职方向（选模板）"))
        self.direction_combo = QComboBox()
        for t in rg.JOB_TEMPLATES:
            self.direction_combo.addItem(t["label"], t["key"])
        self.direction_combo.currentIndexChanged.connect(self._on_direction)
        dir_layout.addWidget(self.direction_combo)
        self.direction_hint = hint_label("")
        self.direction_hint.setWordWrap(True)
        dir_layout.addWidget(self.direction_hint)
        self.example_label = hint_label("")
        self.example_label.setWordWrap(True)
        dir_layout.addWidget(self.example_label)
        form_layout.addWidget(dir_card)

        base_card = card()
        base_layout = QGridLayout(base_card)
        base_layout.setContentsMargins(16, 14, 16, 14)
        base_layout.addWidget(QLabel("姓名"), 0, 0)
        self.name_edit = QLineEdit(placeholderText="张三")
        base_layout.addWidget(self.name_edit, 0, 1)
        base_layout.addWidget(QLabel("求职意向"), 0, 2)
        self.position_edit = QLineEdit(placeholderText="大模型应用开发工程师")
        base_layout.addWidget(self.position_edit, 0, 3)
        base_layout.addWidget(QLabel("手机号"), 1, 0)
        self.phone_edit = QLineEdit()
        base_layout.addWidget(self.phone_edit, 1, 1)
        base_layout.addWidget(QLabel("邮箱"), 1, 2)
        self.email_edit = QLineEdit()
        base_layout.addWidget(self.email_edit, 1, 3)
        base_layout.addWidget(QLabel("GitHub"), 2, 0)
        self.github_edit = QLineEdit()
        base_layout.addWidget(self.github_edit, 2, 1)
        base_layout.addWidget(QLabel("个人总结（可空，AI 帮你写）"), 2, 2)
        self.summary_edit = QLineEdit()
        base_layout.addWidget(self.summary_edit, 2, 3)
        form_layout.addWidget(base_card)

        # 教育经历
        edu_card = card()
        edu_layout = QVBoxLayout(edu_card)
        edu_layout.setContentsMargins(16, 14, 16, 14)
        edu_head = QHBoxLayout()
        edu_head.addWidget(section_label("教育经历"))
        add_edu = primary_button("+ 添加教育")
        add_edu.clicked.connect(lambda: self._add_edu())
        edu_head.addStretch(1)
        edu_head.addWidget(add_edu)
        edu_layout.addLayout(edu_head)
        self.edu_host = QWidget()
        self.edu_layout = QVBoxLayout(self.edu_host)
        self.edu_layout.setContentsMargins(0, 0, 0, 0)
        edu_layout.addWidget(self.edu_host)
        form_layout.addWidget(edu_card)

        # 经历
        exp_card = card()
        exp_layout = QVBoxLayout(exp_card)
        exp_layout.setContentsMargins(16, 14, 16, 14)
        exp_head = QHBoxLayout()
        exp_head.addWidget(section_label("项目 / 实习 / 工作经历"))
        add_exp = primary_button("+ 添加经历")
        add_exp.clicked.connect(lambda: self._add_exp())
        exp_head.addStretch(1)
        exp_head.addWidget(add_exp)
        exp_layout.addLayout(exp_head)
        self.exp_host = QWidget()
        self.exp_layout = QVBoxLayout(self.exp_host)
        self.exp_layout.setContentsMargins(0, 0, 0, 0)
        exp_layout.addWidget(self.exp_host)
        form_layout.addWidget(exp_card)

        # 技能证书
        skill_card = card()
        skill_layout = QVBoxLayout(skill_card)
        skill_layout.setContentsMargins(16, 14, 16, 14)
        skill_layout.addWidget(section_label("技能（点击推荐标签快速添加）"))
        self.skills_host = QWidget()
        self.skills_layout = QVBoxLayout(self.skills_host)
        self.skills_layout.setContentsMargins(0, 0, 0, 0)
        skill_layout.addWidget(self.skills_host)
        self.recommended_host = QWidget()
        self.recommended_layout = QVBoxLayout(self.recommended_host)
        self.recommended_layout.setContentsMargins(0, 0, 0, 0)
        skill_layout.addWidget(self.recommended_host)
        cert_row = QHBoxLayout()
        cert_row.addWidget(QLabel("证书："))
        self.cert_edit = QLineEdit(placeholderText="英语六级、计算机二级…（逗号分隔，可空）")
        cert_row.addWidget(self.cert_edit)
        skill_layout.addLayout(cert_row)
        form_layout.addWidget(skill_card)

        # JD
        jd_card = card()
        jd_layout = QVBoxLayout(jd_card)
        jd_layout.setContentsMargins(16, 14, 16, 14)
        jd_layout.addWidget(section_label("目标 JD（可选，让简历对齐岗位）"))
        self.jd_edit = QPlainTextEdit()
        self.jd_edit.setPlaceholderText("粘贴职位描述原文…也可以先在「岗位分析 · 解析职位」标签解析后回来选")
        self.jd_edit.setFixedHeight(90)
        jd_layout.addWidget(self.jd_edit)
        form_layout.addWidget(jd_card)

        form_scroll = QScrollArea()
        form_scroll.setWidgetResizable(True)
        form_scroll.setWidget(form_host)
        splitter.addWidget(form_scroll)

        # ===== 右侧结果 =====
        result_host = QWidget()
        self.result_layout = QVBoxLayout(result_host)
        self.result_layout.setContentsMargins(8, 0, 0, 0)

        gen_card = card(highlight=True)
        gen_layout = QVBoxLayout(gen_card)
        gen_layout.setContentsMargins(16, 14, 16, 14)
        self.generate_btn = primary_button("生成简历")
        self.generate_btn.clicked.connect(self._generate)
        gen_layout.addWidget(self.generate_btn)
        self.gen_hint = hint_label("生成后可对每段「换 3 个写法」，满意再保存入库。")
        gen_layout.addWidget(self.gen_hint)

        # 撤销 / 重做（覆盖整个表单：删错一张经历卡片、贴错一整段，都能退回来。
        # 输入框自带的 Ctrl+Z 只管单个字段，管不了"卡片被删掉"）
        undo_row = QHBoxLayout()
        undo_row.setSpacing(6)
        self.undo_btn = QPushButton("撤销")
        self.undo_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.undo_btn.clicked.connect(self._undo_step)
        self.redo_btn = QPushButton("重做")
        self.redo_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.redo_btn.clicked.connect(self._redo_step)
        undo_row.addWidget(self.undo_btn)
        undo_row.addWidget(self.redo_btn)
        self.draft_label = hint_label("")
        undo_row.addWidget(self.draft_label, 1)
        gen_layout.addLayout(undo_row)
        self.result_layout.addWidget(gen_card)

        # ---- 生成历史（独立入口：点开看、再点收起；对齐网页版）----
        self.history_card = card()
        hist_lay = QVBoxLayout(self.history_card)
        hist_lay.setContentsMargins(16, 14, 16, 14)
        hist_lay.setSpacing(8)
        hist_head = QHBoxLayout()
        hist_head.setSpacing(8)
        hist_head.addWidget(section_label("生成历史"))
        hist_head.addWidget(hint_label("点击查看，再点收起"))
        hist_head.addStretch(1)
        self.history_refresh_btn = QPushButton("刷新")
        self.history_refresh_btn.setProperty("class", "ghost")
        self.history_refresh_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.history_refresh_btn.clicked.connect(lambda: self._load_history(force=True))
        hist_head.addWidget(self.history_refresh_btn)
        hist_lay.addLayout(hist_head)
        self.history_host = QWidget()
        self.history_layout = QVBoxLayout(self.history_host)
        self.history_layout.setContentsMargins(0, 0, 0, 0)
        self.history_layout.setSpacing(6)
        hist_lay.addWidget(self.history_host)
        self.result_layout.addWidget(self.history_card)

        self.result_card = card()
        self.result_card_layout = QVBoxLayout(self.result_card)
        self.result_card_layout.setContentsMargins(16, 14, 16, 14)
        self.result_host_inner = QWidget()
        self.result_inner = QVBoxLayout(self.result_host_inner)
        self.result_inner.setContentsMargins(0, 0, 0, 0)
        self.result_card_layout.addWidget(self.result_host_inner)
        self.result_layout.addWidget(self.result_card, 1)

        # 生成前占位提示（垂直居中，避免右侧大卡片里一片空白）
        self._placeholder = empty_state(
            "填写左侧信息后，点击上方「生成简历」",
            "生成的简历会在这里预览；可对每一段「换 3 个写法」，满意后保存入库")
        self.result_inner.addWidget(self._placeholder)

        splitter.addWidget(result_host)
        splitter.setSizes([700, 400])
        layout.addWidget(splitter, 1)

        self.overlay = BusyOverlay(self)
        self._on_direction()
        self._add_edu()
        self._add_exp()

        # 表单搭好了：初始状态作为撤销起点，之后每次改动落一次快照。
        self._hook_form_signals()
        self._undo.reset(self._snapshot())
        self._update_undo_buttons()
        self._update_draft_label()
        self._render_history()

        # 快捷键刻意**不用** Ctrl+Z / Ctrl+Y：那是输入框自带的逐字段撤销，
        # 抢过来会让"在某个框里按 Ctrl+Z"变成整个表单回退，比没有还糟。
        self._undo_sc = QShortcut(QKeySequence("Ctrl+Alt+Z"), self,
                                  activated=self._undo_step)
        self._redo_sc = QShortcut(QKeySequence("Ctrl+Alt+Shift+Z"), self,
                                  activated=self._redo_step)

    # ===== 表单快照 / 撤销 / 草稿 =====
    #
    # 为什么用"整表单快照"而不是逐字段撤销：可撤销的状态里包含**卡片的增删**
    # （教育/经历卡片数量是可变的），差异式撤销要为此写逆操作；快照是一个
    # 纯 dict，顺带还能存盘，关掉应用也能把内容还回去。

    def _form_widgets(self) -> list:
        """表单区里参与快照的输入控件（findChildren 顺序 = 创建顺序，稳定）。

        不按属性名收集：本页 ``self.start`` / ``self.end`` 被教育卡片与经历
        卡片各用了一次（后建的覆盖前一个），按属性名会漏掉一半字段。
        """
        host = self.findChild(QWidget, FORM_HOST_NAME)
        if host is None:
            return []
        return [w for w in host.findChildren(QWidget)
                if isinstance(w, (QLineEdit, QPlainTextEdit, QComboBox))]

    @staticmethod
    def _widget_label(widget) -> str:
        """字段的"人话名"：优先占位提示，其次 objectName。

        占位提示是这个表单里最接近字段名的东西（学校 / 专业 / 2023-09…），
        用它做键比用下标可读得多，也便于出问题时对照。
        """
        try:
            text = widget.placeholderText() or ""
        except Exception:                                 # noqa: BLE001
            text = ""
        return (text or widget.objectName() or "").strip()

    @staticmethod
    def _widget_value(widget):
        if isinstance(widget, QPlainTextEdit):
            return widget.toPlainText()
        if isinstance(widget, QComboBox):
            return widget.currentIndex()
        return widget.text()

    def _keyed_widgets(self) -> list:
        """[(键, 控件)]。键 = 类型:占位提示#同类序号（第几个同种同名的）。"""
        counters: dict = {}
        out = []
        for widget in self._form_widgets():
            cls = type(widget).__name__
            label = self._widget_label(widget)
            occ = counters.get((cls, label), 0)
            counters[(cls, label)] = occ + 1
            out.append((f"{cls}:{label}#{occ}", widget))
        return out

    def _snapshot(self) -> dict:
        """当前表单的值快照（含卡片数量与技能列表）。"""
        pairs = self._keyed_widgets()
        return {
            "keys": [k for k, _w in pairs],
            "values": {k: self._widget_value(w) for k, w in pairs},
            "cards": {"edu": len(self._edu_cards), "exp": len(self._exp_cards)},
            "skills": list(self._skills),
        }

    def _sync_card_count(self, kind: str, want: int) -> None:
        """把教育/经历卡片数量调成 want 张（草稿恢复时用）。

        删卡片必须**立刻 setParent(None)**：``deleteLater()`` 只是排队，
        控件在事件循环处理删除之前仍然留在控件树里，``findChildren`` 照样
        能拿到它 —— 那样接着算出来的键列表是错的。
        """
        cards = self._edu_cards if kind == "edu" else self._exp_cards
        layout = self.edu_layout if kind == "edu" else self.exp_layout
        add = self._add_edu if kind == "edu" else self._add_exp
        remove = self._remove_edu if kind == "edu" else self._remove_exp
        want = max(1, int(want or 1))       # 至少留一张，否则用户没地方填
        while len(cards) > want:
            card = cards[-1]
            remove(card)
            layout.removeWidget(card)
            card.setParent(None)
        while len(cards) < want:
            add()

    def _apply_snapshot(self, snap) -> bool:
        """把快照贴回表单；结构对不上就**拒绝**并返回 False。

        结构校验用"键列表必须逐字相同"：对不上说明控件增删过（版本升级改了
        表单、或草稿来自另一套结构）。这时硬贴会把 A 字段的字塞进 B 字段 ——
        宁可放弃这次恢复，也不能给出一个看起来正常、内容错位的表单。
        """
        if not isinstance(snap, dict):
            return False
        values = snap.get("values")
        keys = snap.get("keys")
        if not isinstance(values, dict) or not isinstance(keys, list):
            return False

        cards = snap.get("cards") or {}
        if isinstance(cards, dict):
            try:
                self._sync_card_count("edu", cards.get("edu", 1))
                self._sync_card_count("exp", cards.get("exp", 1))
            except Exception:                             # noqa: BLE001
                return False
            self._hook_form_signals()

        pairs = self._keyed_widgets()
        if [k for k, _w in pairs] != [str(k) for k in keys]:
            return False

        self._restoring = True
        try:
            for key, widget in pairs:
                if key not in values:
                    continue
                value = values[key]
                if isinstance(widget, QPlainTextEdit):
                    widget.setPlainText(str(value or ""))
                elif isinstance(widget, QComboBox):
                    try:
                        widget.setCurrentIndex(int(value))
                    except (TypeError, ValueError):
                        pass
                else:
                    widget.setText(str(value or ""))

            skills = snap.get("skills")
            if isinstance(skills, list):
                # 技能是多选按钮攒出来的（不在控件树里），快照必须单独带一份
                self._skills = [str(s) for s in skills if str(s).strip()]
                self._render_skills()
        finally:
            self._restoring = False
        return True

    def _hook_form_signals(self) -> None:
        """把表单里每个输入控件的变更接到防抖定时器上（幂等，可重复调）。"""
        hooked = getattr(self, "_hooked_ids", None)
        if hooked is None:
            hooked = set()
            self._hooked_ids = hooked
        for widget in self._form_widgets():
            if id(widget) in hooked:
                continue
            hooked.add(id(widget))
            if isinstance(widget, QPlainTextEdit):
                widget.textChanged.connect(self._on_form_changed)
            elif isinstance(widget, QComboBox):
                widget.currentIndexChanged.connect(self._on_form_changed)
            else:
                widget.textChanged.connect(self._on_form_changed)

    def _on_form_changed(self, *_args) -> None:
        if self._restoring:
            return                    # 回填期间的信号不是"用户的改动"
        self._snap_timer.start()

    def _on_form_settled(self) -> None:
        """输入停顿后：落一次撤销快照 + 存一次草稿。"""
        snap = self._snapshot()
        if self._undo.push(snap):
            self._update_undo_buttons()
        local_db.save_draft(DRAFT_SCOPE, snap)
        self._update_draft_label()

    def _undo_step(self) -> None:
        snap = self._undo.undo()
        if snap is None:
            return
        self._apply_snapshot(snap)
        local_db.save_draft(DRAFT_SCOPE, snap)
        self._update_undo_buttons()
        self._update_draft_label("已撤销")

    def _redo_step(self) -> None:
        snap = self._undo.redo()
        if snap is None:
            return
        self._apply_snapshot(snap)
        local_db.save_draft(DRAFT_SCOPE, snap)
        self._update_undo_buttons()
        self._update_draft_label("已重做")

    def _update_undo_buttons(self) -> None:
        self.undo_btn.setEnabled(self._undo.can_undo)
        self.redo_btn.setEnabled(self._undo.can_redo)
        self.undo_btn.setToolTip(
            f"撤销上一次改动（Ctrl+Alt+Z）· 可撤销 {self._undo.undo_depth} 步")
        self.redo_btn.setToolTip(
            f"重做（Ctrl+Alt+Shift+Z）· 可重做 {self._undo.redo_depth} 步")

    def _update_draft_label(self, prefix: str = "") -> None:
        depth = self._undo.undo_depth
        text = f"可撤销 {depth} 步" if depth else ""
        if prefix:
            text = f"{prefix} · {text}" if text else prefix
        self.draft_label.setText(text)

    # ===== 草稿恢复 =====

    def showEvent(self, event) -> None:                  # noqa: N802（Qt 命名）
        super().showEvent(event)
        # 首次进入本页时问一次要不要恢复。放 showEvent 而不是构造期：
        # 页面是懒加载的，构造时用户可能还没登录（本地库是按账号分的）。
        if not self._draft_checked:
            self._draft_checked = True
            QTimer.singleShot(0, self._maybe_restore_draft)
        # 生成历史同理：首次进入才拉，且**不在构造期**发请求
        # （构造期发请求会在 logout→relogin 重建窗口时崩，见 test_ctor_async_audit）。
        if not self._history_loaded:
            self._history_loaded = True
            QTimer.singleShot(0, self._load_history)

    def _maybe_restore_draft(self) -> None:
        try:
            row = local_db.load_draft(DRAFT_SCOPE)
        except Exception:                                 # noqa: BLE001
            return
        if not row:
            return
        snap = row.get("payload")
        values = (snap or {}).get("values") or {}
        if not any(str(v).strip() for v in values.values()):
            # 空草稿（上次打开页面但没填东西）不值得打扰用户
            local_db.clear_draft(DRAFT_SCOPE)
            return
        when = _friendly_time(row.get("updated_at"))
        ret = QMessageBox.question(
            self, "恢复未保存的内容",
            f"发现 {when}未保存的填写内容，要恢复吗？\n\n"
            "选择「否」会丢弃这份草稿。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if ret != QMessageBox.StandardButton.Yes:
            local_db.clear_draft(DRAFT_SCOPE)
            self._update_draft_label()
            return
        if self._apply_snapshot(snap):
            # 恢复完把撤销起点重置到恢复后的状态：否则按一次撤销会跳回空表单
            self._undo.reset(self._snapshot())
            self._update_undo_buttons()
            self._update_draft_label(f"已恢复 {when}的内容")
        else:
            local_db.clear_draft(DRAFT_SCOPE)
            QMessageBox.information(
                self, "无法恢复",
                "这份草稿与当前表单结构不一致（可能是版本更新改过表单），"
                "为避免内容错位已经跳过。")

    def _discard_draft(self) -> None:
        """内容已成功入库/生成：草稿不再是"未保存"的了。"""
        try:
            local_db.clear_draft(DRAFT_SCOPE)
        except Exception:                                 # noqa: BLE001
            pass
        self._update_draft_label()

    # ===== 表单管理 =====

    def _on_direction(self) -> None:
        key = self.direction_combo.currentData()
        for t in rg.JOB_TEMPLATES:
            if t["key"] == key:
                self.direction_hint.setText(f"要点写法：{t['bulletHint']}")
                self.example_label.setText("示例要点：\n" + "\n".join("· " + b for b in t["exampleBullets"]))
                # 推荐技能（流式布局：按可用宽度自动换行，不再固定 6 列）
                while self.recommended_layout.count():
                    item = self.recommended_layout.takeAt(0)
                    old = item.widget()
                    if old is not None:
                        old.setParent(None)
                        old.deleteLater()
                flow = FlowWidget()
                for skill in t["recommendedSkills"]:
                    btn = QPushButton(skill)
                    btn.setStyleSheet(
                        "QPushButton { background:#eef2ff; color:#1d4ed8; border:1px solid #c7d2fe;"
                        "border-radius:12px; padding:3px 10px; font-size:12px; }"
                        "QPushButton:hover { background:#e0e7ff; }"
                    )
                    btn.setCursor(Qt.CursorShape.PointingHandCursor)
                    btn.clicked.connect(lambda _=False, s=skill: self._add_skill(s))
                    flow.add(btn)
                self.recommended_layout.addWidget(flow)
                break
        self._render_skills()

    def _render_skills(self) -> None:
        while self.skills_layout.count():
            item = self.skills_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        if not self._skills:
            self.skills_layout.addWidget(hint_label("尚未添加技能"))
            return
        flow = FlowWidget()
        for skill in self._skills:
            tag = tag_label(skill)
            remove_btn = QPushButton("×")
            remove_btn.setFixedSize(20, 20)
            remove_btn.setStyleSheet(
                "QPushButton { background:transparent; color:#dc2626; border:none; font-weight:700; }"
            )
            remove_btn.setCursor(Qt.CursorShape.PointingHandCursor)
            remove_btn.clicked.connect(lambda _=False, s=skill: self._remove_skill(s))
            pair = QWidget()
            pair_l = QHBoxLayout(pair)
            pair_l.setContentsMargins(0, 0, 0, 0)
            pair_l.setSpacing(4)
            pair_l.addWidget(tag)
            pair_l.addWidget(remove_btn)
            flow.add(pair)
        self.skills_layout.addWidget(flow)

    def _add_skill(self, skill: str) -> None:
        if skill and skill not in self._skills:
            self._skills.append(skill)
            self._render_skills()

    def _remove_skill(self, skill: str) -> None:
        if skill in self._skills:
            self._skills.remove(skill)
            self._render_skills()

    def _add_edu(self) -> None:
        card_widget = EduCard(self._remove_edu)
        self._edu_cards.append(card_widget)
        self.edu_layout.addWidget(card_widget)

    def _remove_edu(self, card_widget) -> None:
        self._edu_cards.remove(card_widget)
        card_widget.deleteLater()

    def _add_exp(self) -> None:
        key = self.direction_combo.currentData()
        hint = next((t["bulletHint"] for t in rg.JOB_TEMPLATES if t["key"] == key), "")
        card_widget = ExpCard(self._remove_exp, hint)
        self._exp_cards.append(card_widget)
        self.exp_layout.addWidget(card_widget)

    def _remove_exp(self, card_widget) -> None:
        self._exp_cards.remove(card_widget)
        card_widget.deleteLater()

    def _collect_request(self) -> dict:
        certs = [c.strip() for c in self.cert_edit.text().split("，") if c.strip()]
        certs += [c.strip() for c in self.cert_edit.text().split(",") if c.strip() and c.strip() not in certs]
        return {
            "name": self.name_edit.text().strip(),
            "position": self.position_edit.text().strip(),
            "phone": self.phone_edit.text().strip(),
            "email": self.email_edit.text().strip(),
            "github": self.github_edit.text().strip(),
            "summary": self.summary_edit.text().strip(),
            "direction": self.direction_combo.currentData(),
            # 只收「填过内容」的卡片：构造期预加了空卡片，无脑全收会让
            # `_generate` 里那条必填校验恒为假（空白表单也能提交，白等 10~30s）。
            "education": [d for d in (c.data() for c in self._edu_cards)
                          if _edu_filled(d)],
            "experiences": [d for d in (c.data() for c in self._exp_cards)
                            if _exp_filled(d)],
            "skills": list(self._skills),
            "certifications": certs,
            "jd_text": self.jd_edit.toPlainText().strip(),
        }

    # ===== 生成 =====

    def _import_resume(self) -> None:
        """导入已有简历（本地提取文本 → 云端入库）。"""
        dlg = ImportResumeDialog(self)
        if dlg.exec() != QDialog.DialogCode.Accepted or not dlg.payload:
            return
        payload = dlg.payload
        self.import_btn.setEnabled(False)
        self._worker = LLMWorker(
            lambda: db.upload_resume(payload["raw_text"], payload["source_file"]))
        self._worker.success.connect(self._on_imported)
        self._worker.failed.connect(self._on_import_failed)
        self._worker.finished.connect(lambda: self.import_btn.setEnabled(True))
        self._worker.start()

    def _on_imported(self, saved) -> None:
        if not isinstance(saved, dict):
            saved = {}
        self.mw.refresh_status()
        ver = saved.get("version")
        QMessageBox.information(
            self, "导入成功",
            "简历已导入%s，可用于「岗位分析 · 匹配度」「简历优化」「面试准备」「薪资谈判」。"
            % (f"（版本 v{ver}）" if ver else ""))

    def _on_import_failed(self, msg: str) -> None:
        QMessageBox.warning(self, "导入失败", str(msg))

    def _generate(self) -> None:
        req = self._collect_request()
        if not req["name"]:
            QMessageBox.warning(self, "缺少信息", "请填写姓名")
            return
        if not req["education"] and not req["experiences"]:
            QMessageBox.warning(self, "缺少信息", "请至少填写一条教育经历或一段经历")
            return
        jd_text = req.pop("jd_text", "")
        # 守门层要拿原始输入核对「生成结果里的数字有没有出处」，缓存一份。
        # JD 也一起锁死在**发起时**的值（见 `__init__` 里的 `_last_jd`）。
        self._last_req = req
        self._last_jd = jd_text
        self.generate_btn.setEnabled(False)
        self.overlay.show_over("AI 正在生成简历…")

        # **本地生成**（不联网）→ 落本地主存；30 分钟后由 services.sync 推到服务器。
        # 生成内部有字数/事实保真/质量三道带反馈的重试，耗时比单次调用长，
        # 所以不写死「约 10~30 秒」这种秒数。
        # 本机模型不可用（没配 Key / 连不上）→ 回落云端：那条路服务端**自己落库**，
        # 不必再存本地，否则同一份简历会同时出现本地与云端两条记录。
        def _run():
            result = rg.generate_resume(req, jd_text)
            if result.get("llm_failed"):
                return db.generate_resume(req, jd_text)
            return rg.save_generated(result)

        self._worker = LLMWorker(_run)
        self._worker.success.connect(self._on_generated)
        self._worker.failed.connect(self._on_generate_failed)
        self._worker.start()

    def _on_generate_failed(self, msg: str) -> None:
        self.overlay.hide()
        self.generate_btn.setEnabled(True)
        QMessageBox.warning(self, "生成失败", msg)

    def _on_generated(self, result: dict) -> None:
        self.overlay.hide()
        self.generate_btn.setEnabled(True)
        if result.get("error"):
            QMessageBox.warning(self, "生成失败", result["error"])
            return
        # 生成已**本地化**（`services.resume_generation.generate_resume`），但守门结论
        # 仍统一在本地补一道：与「改排序 / 换候选」走的 `revalidate` 同一口径，
        # 两条路都收在 `core.generation_guard`，不存在两套判据。
        # JD 用发起时冻结的那份：生成期间用户可能改了输入框，用完成时刻的文本
        # 会让守门判据与实际送进模型的输入对不上。
        result = rg.guard_result(getattr(self, "_last_req", None) or {}, result,
                                 getattr(self, "_last_jd", "") or "")
        self._current_result = result
        self._render_result()
        # 生成历史立刻可见（不等重进页面再拉）；版本链按新版本自己的 id 拉
        self._remember_generated(result)
        self._render_history()
        self._load_versions(result.get("id"))

    def _remember_generated(self, result: dict) -> None:
        """把刚落地的生成结果插到历史列表最前（同一 id 去重）。"""
        rid = str(result.get("id") or "")
        if not rid:
            return
        self._history = [x for x in self._history if str(x.get("id") or "") != rid]
        self._history.insert(0, {
            "id": rid,
            "version": result.get("version"),
            "raw_text": result.get("resume_text") or "",
            "parsed_json": {
                "kind": "generated",
                "generation": dict(result),
                "structured": result.get("structured") or {},
            },
        })

    def _render_result(self) -> None:
        while self.result_inner.count():
            item = self.result_inner.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        result = self._current_result
        if not result or "structured" not in result:
            return
        structured = result["structured"]
        ats = result.get("ats", {})

        # 信息充足度：只在「还不够充足」时提示该补哪块信息（充足时不打扰，
        # 与网页版 ResumeGenerate.tsx 的 level !== "sufficient" 判据一致）
        suff = result.get("sufficiency") or {}
        if suff.get("level") and suff.get("level") != "sufficient":
            suff_card = card(highlight=True)
            sfl = QVBoxLayout(suff_card)
            sfl.setContentsMargins(16, 14, 16, 14)
            sfl.setSpacing(4)
            row = QHBoxLayout()
            s_score = int(suff.get("score") or 0)
            s_title = QLabel(f"信息充足度 {s_score}")
            s_title.setStyleSheet("font-size:13px; font-weight:700; color:#0369a1;")
            row.addWidget(s_title)
            s_msg = QLabel(str(suff.get("message") or ""))
            s_msg.setProperty("class", "text-info")
            s_msg.setWordWrap(True)
            row.addWidget(s_msg, 1)
            sfl.addLayout(row)
            s_gaps = [str(g) for g in (suff.get("gaps") or []) if g]
            for g in s_gaps[:5]:
                gap_item = hint_label(f"· {g}")
                gap_item.setWordWrap(True)
                sfl.addWidget(gap_item)
            if s_gaps:
                sfl.addWidget(hint_label("补齐以上信息后重新生成，简历会更贴合岗位要求。"))
            self.result_inner.addWidget(suff_card)

        # ATS + 字数 + 保真摘要
        summary_card = card(success=(result.get("fidelity") or {}).get("passed", True))
        sl = QVBoxLayout(summary_card)
        sl.setContentsMargins(16, 14, 16, 14)
        head = QHBoxLayout()
        score_label = QLabel(f"ATS {ats.get('score', 0)} 分")
        score_label.setStyleSheet("font-size:20px; font-weight:800; color:#059669;")
        head.addWidget(score_label)
        char_count = len(result.get("resume_text", ""))
        color = "#059669" if char_count >= MIN_RESUME_LENGTH else "#d97706"
        count_label = QLabel(f"全文 {char_count} 字")
        count_label.setStyleSheet(f"font-weight:700; color:{color};")
        head.addWidget(count_label)
        head.addStretch(1)
        sl.addLayout(head)

        fidelity = result.get("fidelity")
        if fidelity:
            if fidelity.get("passed"):
                sl.addWidget(hint_label("事实保真校验通过：未发现编造的技能/证书/联系方式"))
            else:
                warn_parts = []
                if fidelity.get("added_skills"):
                    warn_parts.append("疑似新增技能：" + "、".join(fidelity["added_skills"]))
                if fidelity.get("added_certifications"):
                    warn_parts.append("疑似新增证书：" + "、".join(fidelity["added_certifications"]))
                if fidelity.get("added_contacts"):
                    warn_parts.append("疑似改写联系方式：" + "、".join(fidelity["added_contacts"]))
                warn = QLabel("⚠ " + "；".join(warn_parts))
                warn.setProperty("class", "text-warn")
                warn.setWordWrap(True)
                sl.addWidget(warn)
        self.result_inner.addWidget(summary_card)

        # 内容质量质检（12 条撰写规范，对标成熟简历产品）
        review = result.get("review") or {}
        if review:
            quality_card = card()
            ql = QVBoxLayout(quality_card)
            ql.setContentsMargins(16, 14, 16, 14)
            head = QHBoxLayout()
            score = int(review.get("score", 0))
            tone = "#059669" if score >= 90 else ("#d97706" if score >= 70 else "#dc2626")
            score_label = QLabel(f"内容质量 {score} 分")
            score_label.setStyleSheet(f"font-size:20px; font-weight:800; color:{tone};")
            head.addWidget(score_label)
            head.addWidget(hint_label(f"（{review.get('level', '')}）"))
            head.addStretch(1)
            est = estimate_layout(structured=structured)
            page_text = "预计 1 页 A4" if not est["compact"] else \
                f"预计 {est['pages']} 页，已启用紧凑版式"
            head.addWidget(hint_label(page_text))
            ql.addLayout(head)

            for good in review.get("highlights", [])[:3]:
                ql.addWidget(hint_label("✓ " + good))

            issues = review.get("issues", [])
            for issue in issues[:8]:
                tone = {"high": "#dc2626", "medium": "#d97706"}.get(issue["severity"], "#6b7280")
                row = QLabel(f"· {issue['location']}：{issue['message']}")
                row.setStyleSheet(f"color:{tone};")
                row.setWordWrap(True)
                ql.addWidget(row)
                if issue.get("fix"):
                    fix = QLabel("　→ " + issue["fix"])
                    fix.setProperty("class", "muted")
                    fix.setWordWrap(True)
                    ql.addWidget(fix)
            if len(issues) > 8:
                ql.addWidget(hint_label(f"（另有 {len(issues) - 8} 条改进项）"))
            if not issues:
                ql.addWidget(hint_label("未发现撰写规范问题，可直接导出投递"))
            self.result_inner.addWidget(quality_card)

        # 生成守门（编造硬事实 / 中文求职风险 / AI 腔 / 量化降级建议）
        # 与上面的「事实保真」分工：保真查技能·证书·联系方式这类枚举事实，
        # 守门查散落在正文里的数字、年份、机构名，以及写作层面的风险信号。
        guard = result.get("guard") or {}
        if guard:
            g_issues = guard.get("issues", [])
            has_fabrication = any(
                i.get("category") == "编造" and i.get("severity") == "high"
                for i in g_issues)
            guard_card = card(success=not has_fabrication)
            gl = QVBoxLayout(guard_card)
            gl.setContentsMargins(16, 14, 16, 14)
            g_head = QHBoxLayout()
            g_score = int(guard.get("score", 100))
            g_tone = "#059669" if g_score >= 90 else ("#d97706" if g_score >= 80 else "#dc2626")
            g_title = QLabel(f"生成守门 {g_score} 分")
            g_title.setStyleSheet(f"font-size:20px; font-weight:800; color:{g_tone};")
            g_head.addWidget(g_title)
            if has_fabrication:
                g_flag = QLabel("⚠ 存在疑似编造，投递前必须逐条核对")
                g_flag.setProperty("class", "text-warn")
                g_head.addWidget(g_flag)
            g_head.addStretch(1)
            gl.addLayout(g_head)

            for item in g_issues[:8]:
                g_tone2 = {"high": "#dc2626", "medium": "#d97706"}.get(
                    item.get("severity"), "#6b7280")
                g_row = QLabel(
                    f"· [{item.get('category', '')}] {item.get('location', '')}："
                    f"{item.get('message', '')}")
                g_row.setStyleSheet(f"color:{g_tone2};")
                g_row.setWordWrap(True)
                gl.addWidget(g_row)
                if item.get("fix"):
                    g_fix = QLabel("　→ " + str(item["fix"]))
                    g_fix.setProperty("class", "muted")
                    g_fix.setWordWrap(True)
                    gl.addWidget(g_fix)
            if len(g_issues) > 8:
                gl.addWidget(hint_label(f"（另有 {len(g_issues) - 8} 条）"))
            if not g_issues:
                gl.addWidget(hint_label(
                    "未发现编造、风险或 AI 腔问题；文中的数字都能在你的输入里找到出处"))
            self.result_inner.addWidget(guard_card)

        # 发展连续性（时间线：空窗 / 重叠 / 倒挂）
        cont = (review.get("stats") or {}).get("continuity") or {}
        if cont and cont.get("timeline"):
            cont_card = card(success=cont.get("score", 100) >= 95)
            cl = QVBoxLayout(cont_card)
            cl.setContentsMargins(16, 14, 16, 14)
            c_head = QHBoxLayout()
            c_score = int(cont.get("score", 100))
            c_tone = "#059669" if c_score >= 95 else ("#d97706" if c_score >= 80 else "#dc2626")
            c_title = QLabel(f"发展连续性 {c_score} 分")
            c_title.setStyleSheet(f"font-weight:800; color:{c_tone};")
            c_head.addWidget(c_title)
            c_head.addStretch(1)
            cl.addLayout(c_head)

            timeline = cont.get("timeline") or []
            for item in timeline[:6]:
                period = item.get("raw_period") or "未填写时间"
                kind = item.get("group") or ("教育经历" if item.get("kind") == "education" else "经历")
                cl.addWidget(hint_label(f"　{period}　{item.get('label', '')}　[{kind}]"))

            for gap in (cont.get("gaps") or [])[:3]:
                warn = QLabel(
                    f"⚠ 空窗 {gap['months']} 个月：{gap['after']} → {gap['before']}"
                    f"（{gap['from']} → {gap['to']}）")
                warn.setProperty("class", "text-warn")
                warn.setWordWrap(True)
                cl.addWidget(warn)

            cont_issues = cont.get("issues") or []
            for issue in cont_issues[:4]:
                tone = {"high": "#dc2626", "medium": "#d97706"}.get(issue["severity"], "#6b7280")
                row = QLabel(f"· {issue['location']}：{issue['message']}")
                row.setStyleSheet(f"color:{tone};")
                row.setWordWrap(True)
                cl.addWidget(row)
                if issue.get("fix"):
                    fix = QLabel("　→ " + issue["fix"])
                    fix.setProperty("class", "muted")
                    fix.setWordWrap(True)
                    cl.addWidget(fix)
            if not cont_issues:
                cl.addWidget(hint_label("时间线连贯，未发现空窗、重叠或时间倒挂"))
            self.result_inner.addWidget(cont_card)

        # 表达库改写建议（原句 → 建议句）
        rewrites = review.get("rewrite_suggestions") or []
        if rewrites:
            rw_card = card()
            rl = QVBoxLayout(rw_card)
            rl.setContentsMargins(16, 14, 16, 14)
            rl.addWidget(QLabel("表达优化建议（可直接对照修改）"))
            for item in rewrites:
                origin = QLabel("原句：" + item.get("original", ""))
                origin.setProperty("class", "text-danger")
                origin.setWordWrap(True)
                rl.addWidget(origin)
                if item.get("suggestion"):
                    better = QLabel("建议：" + item["suggestion"])
                    better.setProperty("class", "text-success")
                    better.setWordWrap(True)
                    rl.addWidget(better)
                if item.get("issue"):
                    rl.addWidget(hint_label("　" + item["issue"]))
                if item.get("metrics"):
                    rl.addWidget(hint_label("　可补指标：" + "；".join(item["metrics"])))
                rl.addSpacing(4)
            self.result_inner.addWidget(rw_card)

        # 逐段展示 + 候选按钮
        sortable = [(key, text) for key, text in
                    (("experiences", "经历"), ("education", "教育"))
                    if len(structured.get(key) or []) > 1]
        if sortable:
            order_bar = QWidget()
            ob = QHBoxLayout(order_bar)
            ob.setContentsMargins(0, 4, 0, 0)
            ob.setSpacing(8)
            ob.addWidget(hint_label("调整顺序："))
            for key, label_text in sortable:
                btn = QPushButton(f"{label_text}按时间倒序")
                btn.setStyleSheet(
                    "QPushButton { color:#4f46e5; border:1px solid #d5d8f5;"
                    "border-radius:6px; padding:3px 10px; font-size:12px; }"
                    "QPushButton:hover { background:#eef0fe; }"
                )
                btn.setCursor(Qt.CursorShape.PointingHandCursor)
                btn.clicked.connect(lambda _=False, s=key: self._sort_by_time(s))
                ob.addWidget(btn)
            ob.addStretch(1)
            self.result_inner.addWidget(order_bar)

        sections = []
        if structured.get("summary"):
            sections.append(("个人总结", "summary", None, structured["summary"]))
        for i, edu in enumerate(structured.get("education", [])):
            title = f"教育：{edu.get('school', '')}"
            sections.append((title, "education", i, json.dumps(edu, ensure_ascii=False)))
        for i, exp in enumerate(structured.get("experiences", [])):
            title = f"{exp.get('name', '')}（{exp.get('role', '')}）"
            sections.append((title, "experiences", i, json.dumps(exp, ensure_ascii=False)))
        if structured.get("skills"):
            sections.append(("技能", "skills", None, "、".join(structured["skills"])))

        for title, section, index, preview in sections:
            row = card()
            rl = QHBoxLayout(row)
            rl.setContentsMargins(12, 10, 12, 10)
            label = QLabel(preview if len(preview) < 90 else preview[:90] + "…")
            label.setWordWrap(True)
            rl.addWidget(label, 1)
            if section in ("education", "experiences") and index is not None:
                for arrow, delta in (("↑", -1), ("↓", 1)):
                    move_btn = QPushButton(arrow)
                    move_btn.setFixedWidth(28)
                    move_btn.setToolTip("上移" if delta < 0 else "下移")
                    move_btn.setCursor(Qt.CursorShape.PointingHandCursor)
                    move_btn.clicked.connect(
                        lambda _=False, s=section, i=index, d=delta: self._move_item(s, i, d))
                    rl.addWidget(move_btn)
            regen_btn = QPushButton("换 3 个写法")
            regen_btn.setStyleSheet(
                "QPushButton { color:#7c3aed; border:1px solid #ddd6fe; border-radius:6px;"
                "padding:3px 10px; font-size:12px; } QPushButton:hover { background:#f5f3ff; }"
            )
            regen_btn.setCursor(Qt.CursorShape.PointingHandCursor)
            regen_btn.clicked.connect(
                lambda _=False, s=section, i=index: self._regenerate(s, i))
            rl.addWidget(regen_btn)
            self.result_inner.addWidget(row)

        if result.get("gap"):
            gap = result["gap"]
            gap_row = QWidget()
            gl = QHBoxLayout(gap_row)
            gl.setContentsMargins(0, 6, 0, 0)
            if gap.get("matched"):
                gl.addWidget(flow_tags(gap["matched"], "#059669"), 1)
            if gap.get("missing"):
                gl.addWidget(flow_tags(gap["missing"], "#dc2626"), 1)
            self.result_inner.addWidget(gap_row)

        # 召回轨迹：让「这次到底查了库没有、查到了什么」在界面上可见 ——
        # 检索此前是黑盒，参考为空时用户只会猜「模型不行」。
        retr = retrieval_summary(result.get("retrieval"))
        if retr:
            self.result_inner.addWidget(hint_label(retr))

        for tip in result.get("tips", [])[:5]:
            self.result_inner.addWidget(hint_label("💡 " + tip))

        # 简历全文 + 保存
        text_card = card()
        tl = QVBoxLayout(text_card)
        tl.setContentsMargins(16, 14, 16, 14)
        tl.addWidget(section_label("简历全文"))
        full_text = QLabel(result.get("resume_text", ""))
        full_text.setWordWrap(True)
        full_text.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        tl.addWidget(full_text)
        btn_row = QHBoxLayout()
        save_btn = primary_button("保存入库（可随时在「简历优化」中使用）")
        save_btn.clicked.connect(self._save)
        btn_row.addWidget(save_btn)
        export_btn = primary_button("导出 Word / PDF（可选版式）")
        export_btn.clicked.connect(self._export_with_picker)
        btn_row.addWidget(export_btn)
        export_txt_btn = primary_button("导出纯文本")
        export_txt_btn.clicked.connect(lambda: self._export("txt"))
        btn_row.addWidget(export_txt_btn)
        export_json_btn = primary_button("导出 JSON Resume")
        export_json_btn.setToolTip(
            "导出为 JSON Resume v1.0.0 标准格式 —— 可导入 jsonresume.org、"
            "其它简历工具或平台")
        export_json_btn.clicked.connect(lambda: self._export("json"))
        btn_row.addWidget(export_json_btn)
        btn_row.addStretch(1)
        tl.addLayout(btn_row)
        self.result_inner.addWidget(text_card)
        self.result_inner.addStretch(1)

    # ===== 生成历史 / 版本链（对齐网页版 ResumeGenerate 的「生成历史」卡片）=====
    #
    # 网页版这份数据来自 `api.resume.list()` 里 `parsed_json.kind === "generated"`
    # 的那些记录，按版本倒序；点一条 = 预览那条的生成结果，再点 = 收起。
    # 桌面版此前完全没有这块入口：生成完只能看到「当次」结果，关掉页面
    # 就再也回不到上一份。这里把入口补齐，并**只做只读预览** ——
    # 历史版本不可改（与网页版那句提示语义一致）。

    def _load_history(self, force: bool = False) -> None:
        if self._history_worker is not None and not force:
            return
        self.history_refresh_btn.setEnabled(False)
        worker = LLMWorker(lambda: db.list_resumes())
        worker.success.connect(self._on_history_loaded)
        worker.failed.connect(self._on_history_failed)
        worker.start()
        self._history_worker = worker

    def _on_history_loaded(self, items) -> None:
        self.history_refresh_btn.setEnabled(True)
        self._history = _generated_history(items)
        self._render_history()

    def _on_history_failed(self, msg: str) -> None:
        self.history_refresh_btn.setEnabled(True)
        self._history = []
        self._render_history(str(msg))

    def _render_history(self, error: str = "") -> None:
        while self.history_layout.count():
            item = self.history_layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.setParent(None)
                w.deleteLater()
        if error:
            self.history_layout.addWidget(
                hint_label(f"读取生成历史失败：{error}（点右上「刷新」重试）"))
            return
        if not self._history:
            self.history_layout.addWidget(
                hint_label("还没有生成记录。生成第一份简历后会出现在这里。"))
            return
        flow = FlowWidget()
        for item in self._history:
            flow.add(self._history_chip(item))
        self.history_layout.addWidget(flow)
        if len(self._versions) > 1:
            self.history_layout.addWidget(self._versions_line())
        self.history_layout.addWidget(hint_label(
            "历史记录只做只读预览，不重跑本地质检（缺少当时的原始输入）。"))

    def _history_chip(self, item: dict) -> QWidget:
        wrap = QWidget()
        row = QHBoxLayout(wrap)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(2)
        label = QPushButton(_history_label(item))
        label.setProperty("class", "chip")          # chip + checked = 选中实心
        label.setCheckable(True)
        label.setChecked(self._is_active(item))
        label.setCursor(Qt.CursorShape.PointingHandCursor)
        label.setToolTip("点击查看这条生成记录（再点收起）")
        label.clicked.connect(lambda _c=False, it=item: self._toggle_history_item(it))
        row.addWidget(label)
        dele = QPushButton("×")
        dele.setProperty("class", "ghost")
        dele.setFixedWidth(28)
        dele.setCursor(Qt.CursorShape.PointingHandCursor)
        dele.setToolTip("删除这条生成记录")
        dele.clicked.connect(lambda _c=False, it=item: self._delete_history(it))
        row.addWidget(dele)
        return wrap

    def _versions_line(self) -> QWidget:
        """当前预览那份文档的版本链：v1 … vN，当前那版标出来。"""
        cur = str((self._current_result or {}).get("id") or "")
        wrap = QWidget()
        lay = QHBoxLayout(wrap)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)
        lay.addWidget(hint_label("当前简历版本："))
        flow = FlowWidget()
        for v in self._versions:
            vid = str(v.get("id") or "")
            text = f"v{v.get('version')}"
            if vid == cur:
                flow.add(tag_label(f"{text}（当前）", "#4f46e5"))
            else:
                btn = QPushButton(text)
                btn.setProperty("class", "chip")
                btn.setCursor(Qt.CursorShape.PointingHandCursor)
                btn.setToolTip("查看这一版（只读预览）")
                btn.clicked.connect(lambda _c=False, _v=v: self._view_version(_v))
                flow.add(btn)
        lay.addWidget(flow, 1)
        lay.addWidget(hint_label(f"共 {len(self._versions)} 版"))
        return wrap

    def _is_active(self, item: dict) -> bool:
        cur = str((self._current_result or {}).get("id") or "")
        if not cur:
            return False
        g = _generation_of(item)
        return cur in (str(item.get("id") or ""), str(g.get("id") or ""))

    def _toggle_history_item(self, item: dict) -> None:
        """点历史条目：没在看就预览它，正在看就收起。"""
        if self._is_active(item):
            self._show_result_placeholder()
            self._render_history()
            return
        self._current_result = _result_from_history(item)
        self._render_result()
        self._render_history()
        self._load_versions(_generation_of(item).get("id"))

    def _show_result_placeholder(self) -> None:
        self._current_result = None
        self._versions = []
        while self.result_inner.count():
            it = self.result_inner.takeAt(0)
            w = it.widget()
            if w is not None:
                w.setParent(None)
                w.deleteLater()
        self.result_inner.addWidget(self._placeholder)

    def _load_versions(self, resume_id) -> None:
        if not resume_id:
            self._versions = []
            self._render_history()
            return
        worker = LLMWorker(lambda: db.list_resume_versions(str(resume_id)))
        worker.success.connect(self._on_versions_loaded)
        worker.failed.connect(self._on_versions_failed)
        worker.start()
        self._versions_worker = worker

    def _on_versions_loaded(self, items) -> None:
        self._versions = list(items or [])
        self._render_history()

    def _on_versions_failed(self, _msg: str) -> None:
        # 版本链拉不到不影响主流程（网页版也是这个语义）
        self._versions = []
        self._render_history()

    def _view_version(self, version: dict) -> None:
        """按某个历史版本预览（只读；不写库、不回滚）。"""
        pj = version.get("parsed_json") if isinstance(version.get("parsed_json"), dict) else {}
        g = pj.get("generation") if isinstance(pj.get("generation"), dict) else {}
        self._current_result = _result_from_history({
            "id": version.get("id"),
            "version": version.get("version"),
            "raw_text": version.get("raw_text"),
            "parsed_json": {"kind": "generated", "generation": g,
                            "structured": pj.get("structured")},
        })
        self._render_result()
        self._render_history()

    def _delete_history(self, item: dict) -> None:
        rid = str(item.get("id") or "")
        if not rid:
            return
        ret = QMessageBox.question(
            self, "删除生成记录",
            f"确定删除这条生成记录「{_history_label(item)}」吗？删除后不可恢复。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if ret != QMessageBox.StandardButton.Yes:
            return
        worker = LLMWorker(lambda: db.delete_resume(rid))
        worker.success.connect(lambda _r, it=item: self._on_history_deleted(it))
        worker.failed.connect(lambda m: QMessageBox.warning(self, "删除失败", str(m)))
        worker.start()
        self._delete_worker = worker

    def _on_history_deleted(self, item: dict) -> None:
        was_active = self._is_active(item)
        self._history = [x for x in self._history if x.get("id") != item.get("id")]
        if was_active:
            self._show_result_placeholder()
        self._render_history()

    # ===== 模块排序 =====

    def _move_item(self, section: str, index: int, delta: int) -> None:
        structured = (self._current_result or {}).get("structured") or {}
        items = structured.get(section)
        if not isinstance(items, list):
            return
        target = index + delta
        if 0 <= target < len(items):
            items[index], items[target] = items[target], items[index]
            self._refresh_after_edit()

    def _sort_by_time(self, section: str) -> None:
        structured = (self._current_result or {}).get("structured") or {}
        items = structured.get(section)
        if not isinstance(items, list) or len(items) < 2:
            return
        items.sort(key=_period_key, reverse=True)
        self._refresh_after_edit()

    def _source_text(self) -> str:
        """用户原始输入的纯文本，供守门层核对生成结果是否有依据。"""
        req = getattr(self, "_last_req", None) or {}
        try:
            return rg.source_text_of(req)
        except Exception:  # noqa: BLE001
            return ""

    def _refresh_after_edit(self) -> None:
        """结构改动后重新渲染与评分（不调 LLM）。"""
        self._revalidate_current()

    def _revalidate_current(self) -> None:
        """就地重算当前结果：**合并**而非整体替换。

        ``revalidate`` 只回 ``structured/resume_text/ats/gap/review/guard``；早期
        调用方把返回值**直接赋给** ``_current_result``，于是 ``id`` / ``version`` /
        ``tips`` / ``fidelity`` / ``sufficiency`` 全被丢掉 —— 换过候选或调过排序后，
        点「换 3 个写法」会因为 `_regenerate` 取不到 ``id`` 而失败，
        「信息充足度」卡片也会消失。
        """
        if not self._current_result:
            return
        structured = self._current_result["structured"]
        jd_text = self.jd_edit.toPlainText().strip()
        fresh = rg.revalidate(structured, jd_text,
                              template=self._effective_template(),
                              section_order=self._order,
                              source_text=self._source_text())
        merged = dict(self._current_result)
        merged.update(fresh)
        self._current_result = merged
        self._render_result()

    def _regenerate(self, section: str, index) -> None:
        if not self._current_result:
            return
        structured = self._current_result["structured"]
        jd_text = self.jd_edit.toPlainText().strip()
        self.overlay.show_over("AI 正在生成 3 个候选写法…")

        def do():
            # 本地生成候选（`structured` 即事实来源：旧数字保留不算新增、凭空冒出的才净化）
            return rg.regenerate_section_variants(structured, section, index, jd_text)

        self._worker = LLMWorker(do)
        self._worker.success.connect(lambda cands: self._on_variants(section, index, cands))
        self._worker.failed.connect(self._on_variant_failed)
        self._worker.start()

    def _on_variant_failed(self, msg: str) -> None:
        self.overlay.hide()
        QMessageBox.warning(self, "生成候选失败", msg)

    def _on_variants(self, section: str, index, candidates) -> None:
        self.overlay.hide()
        if not candidates:
            QMessageBox.information(self, "没有候选", "AI 未返回候选，请重试")
            return
        dlg = CandidatesDialog(section, candidates, self)
        if dlg.exec() == QDialog.DialogCode.Accepted and dlg.chosen is not None:
            structured = self._current_result["structured"]
            if section == "summary":
                structured["summary"] = dlg.chosen
            elif section == "skills" and isinstance(dlg.chosen, list):
                structured["skills"] = dlg.chosen
            elif index is not None and isinstance(dlg.chosen, dict):
                structured[section][index] = dlg.chosen
            # 重新渲染评分（不调 LLM）；合并保留 id/version/tips 等元数据
            self._revalidate_current()

    def _save(self) -> None:
        if not self._current_result:
            return
        result = self._current_result
        rid = result.get("id")
        # 内容已经落到云端了，本机那份草稿不再是"未保存"的状态
        self._discard_draft()
        QMessageBox.information(
            self, "已保存",
            f"简历已由云端自动保存（ID: {(rid or '')[:8]}…）\n可在「简历优化」中继续针对岗位优化，或在投递时关联。",
        )

    def _export_note(self) -> str:
        """导出成功提示里附上「用了哪套版式 + 外观 + 顺序」。

        只把**非默认**的项列出来：默认强调色、100% 字号、默认顺序都不提，
        否则每次导出都挂一长串，用户反而看不出这次真正改了什么。
        """
        tpl = self._effective_template()
        parts = [f"版式：{tpl.label}"]
        look = self._appearance
        if look.accent_hex:
            parts.append(f"强调色 #{look.accent_hex}")
        if abs(look.font_scale - 1.0) > 1e-6:
            parts.append(f"字号 {int(round(look.font_scale * 100))}%")
        if list(self._order) != list(BODY_SECTIONS):
            parts.append("顺序：" + " → ".join(section_labels_in_order(self._order)))
        return "\n\n" + "　|　".join(parts)

    def _effective_template(self):
        """当前版式叠加当前外观后的有效模板，供 ATS 与导出使用。"""
        return templates.apply_appearance(
            templates.get_template(self._template_key), self._appearance)

    def _export_with_picker(self) -> None:
        """先选版式（右侧直接看真实 PDF 预览），再导出 Word 或 PDF。"""
        if not self._current_result:
            return
        structured = self._current_result.get("structured") or {}
        dlg = TemplatePickerDialog(structured, self, default_key=self._template_key,
                                   default_appearance=self._appearance,
                                   default_order=self._order)
        code = dlg.exec()
        if code in (TemplatePickerDialog.RESULT_DOCX,
                    TemplatePickerDialog.RESULT_PDF):
            self._template_key = dlg.selected_key
            self._appearance = dlg.selected_appearance
            self._order = dlg.selected_order
            save_export_options(self._appearance, self._order)
            # 版式/外观/顺序都会影响 ATS 结论，所以导出前用新设置重算一次，
            # 免得界面上还挂着上一套设置算出来的分数。
            # 重算失败不能挡住导出本身：用户点的是「导出」，不是「重算」。
            try:
                self._refresh_after_edit()
            except Exception:
                pass
            self._export("docx" if code == TemplatePickerDialog.RESULT_DOCX else "pdf")

    def _export(self, fmt: str) -> None:
        """导出生成结果。docx / pdf 走结构化直出保留版式；json 走 JSON Resume 交换格式。"""
        if not self._current_result:
            return
        result = self._current_result
        # 占位符拦截放在选路径之前：让用户先补数字，而不是选完路径才被弹窗打断
        if not confirm_placeholders(self, result.get("resume_text", "")):
            return
        structured = result.get("structured") or {}
        name = structured.get("name") or "简历"
        position = structured.get("position") or ""
        stem = f"{name}_{position}" if position else name
        default = default_export_path(local_db.resolve_export_dir(), stem, fmt)
        path, _ = QFileDialog.getSaveFileName(self, "导出简历", default, f"文件 (*.{fmt})")
        if not path:
            return
        try:
            if fmt == "docx":
                export_docx(result.get("resume_text", ""), path, name,
                            structured=structured, template_key=self._template_key,
                            order=self._order, appearance=self._appearance)
            elif fmt == "pdf":
                export_pdf(result.get("resume_text", ""), path, name,
                           structured=structured, template_key=self._template_key,
                           order=self._order, appearance=self._appearance)
            elif fmt == "json":
                # 自己编码再写字节：Windows 的文本模式会把 \n 翻成 \r\n，
                # 写出去的文件与内存里的字符串就不再一致（ICS 导出那次踩过同一个坑）。
                payload = json_resume.dumps(structured)
                with open(path, "wb") as fh:
                    fh.write(payload.encode("utf-8"))
            else:
                export_txt(result.get("resume_text", ""), path)
            extra = ""
            if fmt in ("docx", "pdf"):
                extra = self._export_note()
            elif fmt == "json":
                extra = "\n（JSON Resume v1.0.0 —— 可导入 jsonresume.org 或其它平台）"
            # 生成页没有"优化前文本"，结构化数据就是唯一的期望来源，
            # 所以走 verify_exported_pdf（它在内部做 from_structured）。
            verify = None
            if fmt == "pdf":
                try:
                    verify = pdf_verify.verify_exported_pdf(path, structured)
                except Exception:
                    verify = None
            elif fmt == "json":
                # 与 PDF 同一个理念：**从磁盘读回来**再判，不信任内存里那份。
                # 它不需要外部解析引擎，所以不存在 degraded 的情况。
                try:
                    with open(path, "rb") as fh:
                        verify = json_resume.verify_export(
                            json_resume.loads(fh.read().decode("utf-8")))
                except Exception:
                    verify = None
            notify_export_result(self, path, extra, verify, kind=fmt)
        except Exception as exc:
            QMessageBox.warning(self, "导出失败", str(exc))