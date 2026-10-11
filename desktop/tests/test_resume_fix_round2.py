#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""简历生成 / 优化缺陷修复（2026-10-09 第二轮）的判据。

覆盖清单里**桌面侧**的条目（服务端条目在 offerflow-server 侧，另测）：

  D1  守门层取「经历标题」时只认 `left` 键 → 外包 / 玩具项目两条判据**恒不触发**
  D2  `revalidate` 返回值**整体替换** `_current_result` → 丢 id/version/tips/sufficiency
  D3  历史回读读 `quality` 键、写入却在 `review` 键 → 本次会话的记录看不到「内容质量」
  D5  `pres.get('score', 0):.0%` 在值为 `None` 时抛 TypeError → 结果区空白
  D6  构造期预加的空卡片使必填校验恒假 → 空白表单也能提交
  D7  守门取「完成时刻」的 JD，而生成用的是**发起时**捕获的那份
  D8  优化链路不跑 `generation_guard` → 整份重写后没有任何本地编造检查

判据写法遵循既有铁律：取值先看**真实键名**（D1 的教训就是判据读了一个不存在的键），
常量引用真值（`OUTSOURCE_LABELS` / `TOY_PROJECT_WORDS`）而非抄字面量。
反向对照见 `_mutate_fix2_check.py`（把缺陷放回去，看这些用例是否变红）。

运行：python tests/test_resume_fix_round2.py [TestCase.test_method]
"""

import os
import sys
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from PySide6.QtWidgets import QApplication, QDialog, QWidget            # noqa: E402

_APP = QApplication.instance() or QApplication([])

from offerflow_desktop.core import generation_guard as guard             # noqa: E402
from offerflow_desktop.ui.pages import resume_generate as rgp            # noqa: E402
from offerflow_desktop.ui.pages import resume_optimize as rop            # noqa: E402


class _Sig:
    def __init__(self):
        self.slots = []

    def connect(self, fn):
        self.slots.append(fn)

    def emit(self, *args):
        for fn in list(self.slots):
            fn(*args)


class _SyncWorker:
    """同步假 worker：立即执行，不起线程、不打真实接口。"""

    def __init__(self, fn):
        self._fn = fn
        self.success = _Sig()
        self.failed = _Sig()
        self.finished = _Sig()

    def start(self):
        try:
            self.success.emit(self._fn())
        except Exception as exc:          # noqa: BLE001 —— 对齐 LLMWorker.run
            self.failed.emit(str(exc))
        finally:
            self.finished.emit()


class _FakeMW(QWidget):
    def navigate(self, *args, **kwargs):
        pass

    def refresh_status(self):
        pass

    def open_agent(self, *args, **kwargs):
        pass


def _structured(**over):
    base = {
        "name": "张三", "position": "后端开发", "contact": {},
        "summary": "3 年后端开发经验", "education": [], "skills": [],
        "certifications": [], "experiences": [],
    }
    base.update(over)
    return base


# ============ D1：守门层取经历标题 ============


class TestExpLabel(unittest.TestCase):
    """`_exp_label` 必须同时认 `name`/`role` 与 `left` 两套形状。"""

    def test_name_role_shape(self):
        exp = {"name": "某科技有限公司", "role": "后端开发", "bullets": []}
        self.assertEqual(guard._exp_label(exp), "某科技有限公司 后端开发")

    def test_name_only(self):
        self.assertEqual(guard._exp_label({"name": "某科技", "role": ""}), "某科技")

    def test_left_shape_still_supported(self):
        """`left` 是 parse_resume_text 那套解析块的键，导出链路在用，不能丢。"""
        self.assertEqual(guard._exp_label({"left": "解析块标题", "name": "X"}), "解析块标题")

    def test_non_dict_is_empty(self):
        self.assertEqual(guard._exp_label(None), "")
        self.assertEqual(guard._exp_label("不是字典"), "")


class TestRiskGuardOnGeneratedShape(unittest.TestCase):
    """生成链路给的是 name/role 形状 —— 两条判据必须真的能被触发。"""

    # 取真值里的第一个词，避免抄字面量后常量变了判据还"绿着"
    OUTSOURCE = guard.OUTSOURCE_LABELS[0]
    TOY = guard.TOY_PROJECT_WORDS[0]

    def _risk(self, structured):
        report = guard.check(structured, structured.get("summary") or "简历正文",
                             source_text="用户填写的原始材料", jd_text="")
        return [i for i in report["issues"] if i["category"] == "风险"]

    def test_outsource_label_fires_on_name_role(self):
        issues = self._risk(_structured(experiences=[
            {"name": self.OUTSOURCE, "role": "后端开发", "type": "work", "bullets": []},
        ]))
        self.assertTrue(any(self.OUTSOURCE in i["message"] for i in issues),
                        "name/role 形状下外包判据没有触发：%s" % issues)

    def test_toy_project_fires_on_name_role(self):
        issues = self._risk(_structured(experiences=[
            {"name": "电商%s系统" % self.TOY, "role": "开发", "type": "project", "bullets": []},
        ]))
        self.assertTrue(any("练习型项目" in i["message"] for i in issues),
                        "name/role 形状下玩具项目判据没有触发：%s" % issues)

    def test_left_shape_still_fires(self):
        """解析块形状（left）不能被这次改动弄坏。"""
        issues = self._risk(_structured(experiences=[
            {"left": self.OUTSOURCE, "bullets": []},
        ]))
        self.assertTrue(any(self.OUTSOURCE in i["message"] for i in issues))

    def test_reverse_control_old_behaviour_is_blind(self):
        """反向对照：把取值换回「只认 left」，两条判据应立刻变哑。"""
        structured = _structured(experiences=[
            {"name": self.OUTSOURCE, "role": "后端开发", "type": "work", "bullets": []},
        ])
        with mock.patch.object(guard, "_exp_label",
                               lambda exp: str((exp or {}).get("left") or "")):
            issues = self._risk(structured)
        self.assertFalse(any(self.OUTSOURCE in i["message"] for i in issues),
                         "反向对照失败：旧取值居然也能触发判据")


# ============ D3：历史回读的键 ============


class TestHistoryReviewKey(unittest.TestCase):
    def test_reads_review_key(self):
        """写入侧存的是 `review`（guard_result / api_client 都用这个键）。"""
        item = {"id": "r1", "version": 2, "raw_text": "文本",
                "parsed_json": {"kind": "generated",
                                "generation": {"id": "r1",
                                               "review": {"score": 88, "level": "优秀"}}}}
        out = rgp._result_from_history(item)
        self.assertEqual(out["review"], {"score": 88, "level": "优秀"})

    def test_still_reads_quality_key(self):
        """服务端原始形状（quality）也要继续认。"""
        item = {"id": "r1", "version": 2, "raw_text": "文本",
                "parsed_json": {"kind": "generated",
                                "generation": {"id": "r1",
                                               "quality": {"score": 70, "level": "良好"}}}}
        self.assertEqual(rgp._result_from_history(item)["review"],
                         {"score": 70, "level": "良好"})

    def test_absent_is_empty_dict(self):
        item = {"id": "r1", "version": 1, "raw_text": "文本",
                "parsed_json": {"kind": "generated", "generation": {"id": "r1"}}}
        self.assertEqual(rgp._result_from_history(item)["review"], {})


# ============ 页面级：D2 / D5 / D6 / D7 / D8 ============


class _Base(unittest.TestCase):
    def _gen_page(self):
        page = rgp.ResumeGeneratePage(_FakeMW())
        self.addCleanup(page.deleteLater)
        return page

    def _opt_page(self):
        page = rop.ResumeOptimizePage(_FakeMW())
        self.addCleanup(page.deleteLater)
        return page


class TestRevalidateMerge(_Base):
    """D2：`revalidate` 只回一部分字段，元数据必须保留。"""

    STRUCT = {"name": "张三", "experiences": [{"name": "A", "bullets": ["b"]}],
              "skills": [], "education": [], "certifications": [],
              "summary": "", "position": "", "contact": {}}
    FRESH = {"structured": STRUCT, "resume_text": "张三",
             "ats": {"score": 80}, "gap": {}, "review": {"score": 70},
             "guard": {"score": 90, "passed": True, "issues": []}}

    def _page_with_result(self):
        page = self._gen_page()
        page._current_result = {
            "id": "rid-1", "version": 3, "tips": ["已有提示"],
            "sufficiency": {"level": "ok"}, "fidelity": {"passed": True},
            "structured": self.STRUCT, "resume_text": "旧文本",
        }
        return page

    def test_refresh_after_edit_keeps_metadata(self):
        page = self._page_with_result()
        with mock.patch.object(rgp.rg, "revalidate", return_value=dict(self.FRESH)):
            page._refresh_after_edit()
        cur = page._current_result
        self.assertEqual(cur["id"], "rid-1")
        self.assertEqual(cur["version"], 3)
        self.assertEqual(cur["tips"], ["已有提示"])
        self.assertEqual(cur["sufficiency"], {"level": "ok"})
        self.assertEqual(cur["resume_text"], "张三")           # 新值要覆盖
        self.assertEqual(cur["ats"], {"score": 80})

    def test_variants_path_keeps_metadata(self):
        """`_on_variants` 是同一个缺陷的第二个落点。"""
        page = self._page_with_result()

        class _Dialog:
            def __init__(self, *args, **kwargs):
                self.chosen = "改写后的总结"

            def exec(self):
                return QDialog.DialogCode.Accepted

        with mock.patch.object(rgp, "CandidatesDialog", _Dialog), \
             mock.patch.object(rgp.rg, "revalidate", return_value=dict(self.FRESH)):
            page._on_variants("summary", None, ["改写后的总结"])
        self.assertEqual(page._current_result["id"], "rid-1")
        self.assertEqual(page._current_result["sufficiency"], {"level": "ok"})


class TestOptimizePercent(_Base):
    """D5：比例值是 None 时不能把 `_on_optimized` 打断。"""

    def test_pct_helper(self):
        self.assertEqual(rop._pct(0.5), "50%")
        self.assertEqual(rop._pct(None), "—")
        self.assertEqual(rop._pct(""), "—")
        self.assertEqual(rop._pct("没法转"), "—")

    def test_null_score_does_not_break_result_area(self):
        page = self._opt_page()
        page._resume = {"raw_text": "负责系统优化"}
        page._jd = {"raw_text": "后端 JD"}
        page._on_optimized({
            "optimized_text": "负责系统优化",
            "preservation": {"passed": True, "score": None},
            "changes": [], "gap": {},
        })
        self.assertIn("保留度", page.status_label.text())
        self.assertNotIn("50%", page.status_label.text())      # 不能把 None 当成 0


class TestEmptyCardValidation(_Base):
    """D6：构造期预加的空卡片不算「填过」。"""

    def test_pure_helpers(self):
        self.assertFalse(rgp._edu_filled({"school": "", "major": "", "degree": "本科"}))
        self.assertTrue(rgp._edu_filled({"school": "某大学"}))
        self.assertFalse(rgp._exp_filled({"name": "", "role": "", "bullets": []}))
        self.assertTrue(rgp._exp_filled({"bullets": ["做了什么"]}))

    def test_blank_form_collects_nothing(self):
        page = self._gen_page()
        req = page._collect_request()
        self.assertEqual(req["education"], [])
        self.assertEqual(req["experiences"], [])

    def test_degree_default_alone_is_not_content(self):
        page = self._gen_page()
        page._edu_cards[0].degree.setCurrentText("硕士")
        self.assertEqual(page._collect_request()["education"], [])

    def test_blank_form_blocks_generate(self):
        """空白表单点生成 → 直接提示，不该白等 10~30 秒。"""
        page = self._gen_page()
        page.name_edit.setText("张三")
        with mock.patch.object(rgp, "QMessageBox") as box:
            page._generate()
        box.warning.assert_called_once()

    def test_filled_experience_is_collected(self):
        page = self._gen_page()
        page._exp_cards[0].name.setText("某项目")
        page._exp_cards[0].bullets.setPlainText("做了 A\n做了 B")
        got = page._collect_request()["experiences"]
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0]["name"], "某项目")
        self.assertEqual(got[0]["bullets"], ["做了 A", "做了 B"])


class TestFrozenJd(_Base):
    """D7：守门层核对用的是发起时那份 JD。"""

    def test_generate_freezes_jd(self):
        page = self._gen_page()
        page.name_edit.setText("张三")
        page._exp_cards[0].name.setText("某项目")
        page.jd_edit.setPlainText("发起时的JD")
        with mock.patch.object(rgp, "LLMWorker", _SyncWorker), \
             mock.patch.object(rgp.db, "generate_resume",
                               return_value={"resume_text": "t", "structured": {}}):
            page._generate()
        self.assertEqual(page._last_jd, "发起时的JD")

    def test_on_generated_uses_frozen_jd(self):
        page = self._gen_page()
        page._last_req = {"name": "张三"}
        page._last_jd = "冻结JD"
        page.jd_edit.setPlainText("完成后改的JD")
        seen = {}

        def fake_guard_result(req, result, jd_text=""):
            seen["jd"] = jd_text
            return result

        page._render_history = lambda *a, **k: None
        page._load_versions = lambda *a, **k: None
        with mock.patch.object(rgp.rg, "guard_result", side_effect=fake_guard_result):
            page._on_generated({"structured": {"name": "张三"}, "resume_text": "t"})
        self.assertEqual(seen["jd"], "冻结JD")


class TestGuardText(_Base):
    """D8：优化链路（纯文本产物）也要跑守门层。"""

    def test_flags_unsourced_numbers(self):
        report = rgp.rg.guard_text(
            "负责系统优化，响应时间降低 40%，支撑 5000 QPS", "负责系统优化", "后端")
        self.assertFalse(report["passed"])
        self.assertTrue(any(i["category"] == "编造" for i in report["issues"]))

    def test_number_in_source_passes(self):
        text = "负责系统优化，响应时间降低 40%"
        report = rgp.rg.guard_text(text, text, "")
        self.assertEqual([i for i in report["issues"] if i["category"] == "编造"], [])

    def test_optimize_page_renders_guard_section(self):
        page = self._opt_page()
        page._resume = {"raw_text": "负责系统优化"}
        page._jd = {"raw_text": "后端 JD"}
        page._on_optimized({
            "optimized_text": "负责系统优化，响应时间降低 40%，支撑 5000 QPS",
            "preservation": {"passed": True, "score": 0.9},
            "changes": [], "gap": {},
        })
        text = page.changes_view.toPlainText()
        self.assertIn("编造/风险守门", text)
        # 只断言「区块出现了」是不够的：一个恒返回"通过"的假实现也能让上一行变绿。
        # 这段优化稿里的 40% / 5000 QPS 在原文里没有出处，守门层必须报不通过。
        self.assertIn("未通过", text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
