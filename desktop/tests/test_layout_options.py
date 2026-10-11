"""版式 / 外观 / 分区顺序 的判据。

为什么需要这份判据
------------------
这三件事原来**都不是**可配的：分区顺序在三个地方各写死一份（PDF 一处、
Word 估高一处、Word 渲染一处），配色与字号被绑死在版式对象里。
改成可配之后，最容易出的错不是崩溃，而是**静默分叉**：

* Word 估高按 A 顺序算、渲染按 B 顺序算 ⇒ 「估出来一页、印出来两页」；
* 预览缓存 key 漏掉颜色 ⇒ 改了颜色还显示旧图；
* 「自称 ATS 安全」的字段没人同步 ⇒ 假声明。

这些都不会报错，只会让用户看到不对的东西。所以判据按错题本的取向写：

* 断言基于**结构**（AST 找调用点、docx XML 里数标题顺序、反射取属性），
  不做「源码里有没有这句话」的文本匹配；
* **顺序不变两处取同一来源**用 AST 判据钉住；
* 「ATS 标注是现算的」用「同一模板缩字号后标注从空变非空」来证，
  而不是去断言某个字符串 —— 后者只能证明字面量写对了。
"""
import ast
import os
import re
import sys
import tempfile
import unittest
import zipfile

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from offerflow_desktop.core.ats import check_ats_compatibility
from offerflow_desktop.core.sections import (
    BODY_SECTIONS, RECOMMENDED_SECTION_ORDER, SECTION_LABELS,
    normalize_section_order, section_labels_in_order,
)
from offerflow_desktop.services import exporter, pdf_exporter
from offerflow_desktop.services import templates as T

SAMPLE = {
    "name": "张三",
    "position": "Python 后端",
    "contact": {"phone": "13800000000", "email": "zhangsan@example.com"},
    "summary": "三年后端开发经验，主导过订单服务重构。",
    "education": [{"school": "某大学", "major": "计算机", "degree": "本科",
                   "start": "2019-09", "end": "2023-06"}],
    "experiences": [{"name": "某公司", "role": "后端工程师", "start": "2023-07",
                     "bullets": ["负责订单服务重构，QPS 提升 3 倍"]}],
    "skills": ["Python", "FastAPI", "PostgreSQL"],
}


def _body_source(name):
    """按函数名取出函数体源码（用于 AST 判据）。"""
    out = {}
    for rel in ("services/pdf_exporter.py", "services/exporter.py"):
        path = os.path.join(_ROOT, "offerflow_desktop", rel)
        with open(path, encoding="utf-8") as fh:
            tree = ast.parse(fh.read())
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == name:
                out[rel] = node
    return out


def _calls(func_node, callee_name):
    for sub in ast.walk(func_node):
        if isinstance(sub, ast.Call):
            target = sub.func
            if isinstance(target, ast.Name) and target.id == callee_name:
                return True
            if isinstance(target, ast.Attribute) and target.attr == callee_name:
                return True
    return False


def _docx_heading_sequence(path):
    """从 docx 里按出现顺序取出被识别到的分区标题。"""
    xml = zipfile.ZipFile(path).read("word/document.xml").decode("utf-8")
    texts = re.findall(r"<w:t[^>]*>([^<]*)</w:t>", xml)
    wanted = set(SECTION_LABELS.values())
    return [t for t in texts if t in wanted]


class SectionOrderTests(unittest.TestCase):
    """规律规整：丢非法 / 去重 / 补缺 / 不变量。"""

    def test_unknown_names_are_dropped(self):
        order = normalize_section_order(["skills", "不存在的分区", "summary"])
        self.assertEqual(set(order), set(BODY_SECTIONS))
        self.assertNotIn("不存在的分区", order)

    def test_duplicates_keep_first_position(self):
        order = normalize_section_order(["skills", "summary", "skills", "skills"])
        self.assertEqual(order[0], "skills")
        self.assertEqual(order[1], "summary")
        self.assertEqual(order.count("skills"), 1)

    def test_missing_are_appended_in_default_relative_order(self):
        order = normalize_section_order(["honors"])
        self.assertEqual(order[0], "honors")
        # 其余按 BODY_SECTIONS 的相对顺序补齐
        rest = [n for n in BODY_SECTIONS if n != "honors"]
        self.assertEqual(order[1:], rest)

    def test_always_returns_a_permutation(self):
        """不变量：无论输入多脏，返回值恒是 BODY_SECTIONS 的一个排列。

        渲染循环直接遍历这个返回值，所以这条不变量就是渲染层的安全保证。
        """
        dirty = [
            None, [], "skills", 123, ["", None], ["summary", "summary"],
            ["x" * 500], [1, 2, 3], ["extra", "extra", "nope"],
            list(reversed(BODY_SECTIONS)), [None, "skills", None],
        ]
        for raw in dirty:
            order = normalize_section_order(raw)
            with self.subTest(raw=raw):
                self.assertEqual(len(order), len(BODY_SECTIONS))
                self.assertEqual(set(order), set(BODY_SECTIONS))

    def test_string_input_treated_as_single_name(self):
        """传字符串不是静默回退默认序 —— 那会让调用错误永远查不出来。"""
        order = normalize_section_order("skills")
        self.assertEqual(order[0], "skills")
        self.assertNotEqual(order, list(BODY_SECTIONS))

    def test_labels_follow_the_order(self):
        self.assertEqual(section_labels_in_order(["honors", "skills"])[:2],
                         [SECTION_LABELS["honors"], SECTION_LABELS["skills"]])


class SingleSourceOfOrderTests(unittest.TestCase):
    """顺序必须只有一处定义：三处渲染/度量都从 core.sections 取。"""

    def test_pdf_renderer_consumes_shared_order(self):
        src = _body_source("_render")
        self.assertIn("services/pdf_exporter.py", src, "没找到 pdf 的 _render")
        self.assertTrue(
            _calls(src["services/pdf_exporter.py"], "normalize_section_order"),
            "pdf_exporter._render 必须通过 normalize_section_order 取顺序；"
            "若退回逐个 if 写死，PDF 与 Word 会立刻分叉")

    def test_word_renderer_consumes_shared_order(self):
        src = _body_source("_render_blocks")
        self.assertIn("services/exporter.py", src, "没找到 word 的 _render_blocks")
        self.assertTrue(
            _calls(src["services/exporter.py"], "normalize_section_order"),
            "exporter._render_blocks 必须通过 normalize_section_order 取顺序")

    def test_word_height_estimator_consumes_shared_order(self):
        """估高也必须取同一份顺序。

        这条最容易漏：估高与实际渲染分叉不会报错，只会让「一页约束」判错。
        """
        src = _body_source("_estimate_height")
        self.assertTrue(
            _calls(src["services/exporter.py"], "normalize_section_order"),
            "exporter._estimate_height 必须通过 normalize_section_order 取顺序")

    def test_no_renderer_hardcodes_section_labels(self):
        """渲染器里不应再出现写死的分区标题字面量（标题文案统一从 SECTION_LABELS 取）。"""
        for rel, funcs in (("services/pdf_exporter.py", ("_render",)),
                           ("services/exporter.py", ("_render_blocks",))):
            path = os.path.join(_ROOT, "offerflow_desktop", rel)
            with open(path, encoding="utf-8") as fh:
                tree = ast.parse(fh.read())
            for node in ast.walk(tree):
                if not (isinstance(node, ast.FunctionDef) and node.name in funcs):
                    continue
                literals = {n.value for n in ast.walk(node)
                            if isinstance(n, ast.Constant) and isinstance(n.value, str)}
                hit = literals & set(SECTION_LABELS.values())
                self.assertFalse(hit, f"{rel}:{node.name} 仍写死标题 {hit}；"
                                      "标题应从 SECTION_LABELS 取")


class RenderOrderTests(unittest.TestCase):
    """端到端：顺序真的改变了产出（Word 用 docx XML 数，PDF 用字节口径）。"""

    def test_word_renders_sections_in_requested_order(self):
        custom = ["skills", "experiences", "education", "summary"]
        p1 = os.path.join(tempfile.gettempdir(), "_off_order_a.docx")
        p2 = os.path.join(tempfile.gettempdir(), "_off_order_b.docx")
        exporter.export_docx_from_structured(SAMPLE, p1)
        exporter.export_docx_from_structured(SAMPLE, p2, order=custom)

        default_seq = _docx_heading_sequence(p1)
        custom_seq = _docx_heading_sequence(p2)
        self.assertIn(SECTION_LABELS["summary"], default_seq)
        self.assertIn(SECTION_LABELS["skills"], custom_seq)
        # 自定义顺序里，技能必须排在个人总结之前；默认顺序里相反
        self.assertLess(custom_seq.index(SECTION_LABELS["skills"]),
                        custom_seq.index(SECTION_LABELS["summary"]))
        self.assertGreater(default_seq.index(SECTION_LABELS["skills"]),
                           default_seq.index(SECTION_LABELS["summary"]))
        self.assertNotEqual(default_seq, custom_seq)

    def test_order_does_not_change_total_height(self):
        """总高度对顺序不敏感 —— 一页调优正是建立在这一点上。

        这是**设计前提**的判据：哪天有人给某个分区加了「只在页面顶部生效」
        的特殊逻辑，这条会先变红，提醒他 scale/lead/spread/top_pad 那四段
        判定需要重新审。
        """
        blocks = exporter.from_structured(SAMPLE)
        tpl = T.get_template("classic")
        base = exporter._estimate_height(blocks, tpl)
        for order in (list(reversed(BODY_SECTIONS)),
                      ["skills", "honors", "extra", "summary", "education",
                       "experiences", "certifications"]):
            with self.subTest(order=order):
                self.assertAlmostEqual(
                    exporter._estimate_height(blocks, tpl, order=order), base, places=6)

    def test_pdf_output_reflects_order(self):
        default_bytes, default_layout = pdf_exporter.render_pdf_bytes(SAMPLE)
        custom_bytes, custom_layout = pdf_exporter.render_pdf_bytes(
            SAMPLE, order=["skills", "experiences", "education", "summary"])
        self.assertNotEqual(default_bytes, custom_bytes,
                            "换分区顺序后 PDF 字节应发生变化")
        self.assertEqual(default_layout["fill_ratio"], custom_layout["fill_ratio"],
                         "顺序不该改变填充率（总高度不敏感）")


class AppearanceTests(unittest.TestCase):
    """外观（颜色 / 字号 / 字体）与版式解耦。"""

    def test_apply_appearance_returns_copy(self):
        """绝不能就地改模块级单例 —— 否则这一次导出的颜色会渗进之后所有导出。"""
        before = dict(T.get_template("classic").to_dict())
        T.apply_appearance(T.get_template("classic"),
                           T.ResumeAppearance(accent_hex="FF0000", font_scale=0.9))
        after = dict(T.get_template("classic").to_dict())
        self.assertEqual(before, after, "apply_appearance 污染了模板单例")

    def test_appearance_changes_color_and_scale(self):
        tpl = T.get_template("classic")
        out = T.apply_appearance(tpl, T.ResumeAppearance(accent_hex="0F766E",
                                                        font_scale=0.9))
        self.assertNotEqual(out.accent_hex, tpl.accent_hex)
        self.assertAlmostEqual(out.body_pt, tpl.body_pt * 0.9, places=2)
        self.assertAlmostEqual(out.name_pt, tpl.name_pt * 0.9, places=2)

    def test_appearance_leaves_layout_alone(self):
        """栏数、边距、行距、标题装饰、对齐都属于**版式**，外观不许碰。"""
        tpl = T.get_template("elegant")
        out = T.apply_appearance(tpl, T.ResumeAppearance(accent_hex="123456",
                                                        font_scale=1.15))
        for attr in ("columns", "margin_cm", "line", "heading_style",
                     "name_align", "bullet_char", "font_cn", "font_en"):
            with self.subTest(attr=attr):
                self.assertEqual(getattr(out, attr), getattr(tpl, attr))

    def test_text_color_keeps_contrast_guard(self):
        """换成浅色强调色时，文字用色要自动压深（装饰色仍鲜亮）。"""
        out = T.apply_appearance(T.get_template("classic"),
                                 T.ResumeAppearance(accent_hex="FBBF24"))
        self.assertEqual(out.accent_hex, "FBBF24")
        self.assertNotEqual(out.text_accent_hex, "FBBF24")

    def test_invalid_input_falls_back(self):
        self.assertIsNone(T.ResumeAppearance(accent_hex="not-a-color")
                          .normalized().accent_hex)
        self.assertEqual(T.ResumeAppearance(accent_hex="#12").normalized().accent_hex,
                         None)
        self.assertEqual(T.ResumeAppearance(font_scale=99).normalized().font_scale,
                         T.ResumeAppearance.MAX_SCALE)
        self.assertEqual(T.ResumeAppearance(font_scale=0.1).normalized().font_scale,
                         T.ResumeAppearance.MIN_SCALE)
        self.assertEqual(T.ResumeAppearance(font_scale="x").normalized().font_scale, 1.0)

    def test_dict_round_trip(self):
        look = T.ResumeAppearance(accent_hex="0f766e", font_scale=0.9)
        back = T.ResumeAppearance.from_dict(look.to_dict())
        self.assertEqual(back.normalized().accent_hex, "0F766E")
        self.assertAlmostEqual(back.normalized().font_scale, 0.9, places=3)

    def test_from_dict_rejects_garbage(self):
        self.assertTrue(T.ResumeAppearance.from_dict(None).is_default())
        self.assertTrue(T.ResumeAppearance.from_dict("oops").is_default())
        self.assertTrue(T.ResumeAppearance.from_dict({"font_scale": []}).is_default())

    def test_switch_template_keeps_appearance(self):
        """用户挑好的外观换版式后仍在 —— 这是拆开两条轴的全部意义。"""
        look = T.ResumeAppearance(accent_hex="0F766E", font_scale=0.95)
        a = T.apply_appearance(T.get_template("classic"), look)
        b = T.apply_appearance(T.get_template("tech"), look)
        self.assertEqual(a.accent_hex, b.accent_hex)
        self.assertAlmostEqual(a.body_pt / T.get_template("classic").body_pt,
                               b.body_pt / T.get_template("tech").body_pt,
                               places=3)


class AtsNotesTests(unittest.TestCase):
    """ATS 标注必须是**现算**的，不是手填字段。"""

    def test_notes_are_computed_not_declared(self):
        """同一套版式，缩字号后标注从空变非空 ⇒ 结论是算出来的。

        这条是反向对照的核心：如果有人把 ats_notes 改成手填的固定列表，
        缩字号后它不会变，这里立刻变红。
        """
        tpl = T.get_template("tech")
        self.assertEqual(tpl.ats_notes, [], "出厂技术岗版式不该有可读性风险")
        narrowed = T.apply_appearance(tpl, T.ResumeAppearance(font_scale=0.85))
        self.assertTrue(narrowed.ats_notes,
                        "字号缩到 85% 后必须自动报出「低于 9pt」")
        self.assertTrue(any("9" in n for n in narrowed.ats_notes))

    def test_ats_safe_agrees_with_notes(self):
        for key in T.TEMPLATE_MAP:
            tpl = T.get_template(key)
            with self.subTest(template=key):
                self.assertEqual(tpl.ats_safe, not tpl.ats_notes)

    def test_line_spacing_rule_fires_on_compact(self):
        """紧凑档行距 1.08 < 1.15，应有且仅有一条行距类提醒。"""
        notes = T.get_template("compact").ats_notes
        self.assertTrue(any("行距" in n for n in notes))

    def test_two_column_is_flagged(self):
        """双栏必须报「ATS 可能拆散关键词」。

        双栏由 ``sidebar_sections`` 声明，而不是一个 ``columns=2`` 开关 ——
        没有分区声明的「双栏」只会把整段正文塞进左半页，不是真分栏，
        所以那种情况按单栏处理，也不该报双栏的 ATS 风险（反向对照）。
        """
        tpl = T.get_template("classic")
        wide = T.ResumeTemplate(tpl.key, tpl.label, tpl.desc,
                                sidebar_sections=("skills",))
        self.assertEqual(wide.columns, 2)
        self.assertTrue(any("栏" in n for n in wide.ats_notes))
        # 反向对照：只给 columns=2、不给分区 → 仍是单栏，不报双栏风险
        narrow = T.ResumeTemplate(tpl.key, tpl.label, tpl.desc, columns=2)
        self.assertEqual(narrow.columns, 1)
        self.assertFalse(any("栏" in n for n in narrow.ats_notes))

    def test_template_notes_do_not_change_score(self):
        """版式是我方设计选择，不该扣用户的分。"""
        text = SAMPLE["name"] + " 13800000000 zhangsan@example.com\n" \
            + "个人总结\n技能\n项目经历\n教育经历\n" + "内容" * 120
        plain = check_ats_compatibility(text)
        with_tpl = check_ats_compatibility(text, template=T.get_template("compact"))
        self.assertEqual(plain["score"], with_tpl["score"])
        self.assertEqual(with_tpl["template_notes"],
                         T.get_template("compact").ats_notes)
        self.assertTrue(any("紧凑" in i for i in with_tpl["issues"]))

    def test_order_suggestion_only_when_user_chose_one(self):
        """没显式设过顺序就别提 —— 否则每次检查都挂一句永远成立的废话。"""
        text = SAMPLE["name"] + " 13800000000 z@example.com\n教育经历\n技能\n" + "内容" * 120
        quiet = check_ats_compatibility(text)
        self.assertFalse(any("小节顺序" in s for s in quiet["suggestions"]))
        loud = check_ats_compatibility(text, section_order=["skills", "summary",
                                                           "education"])
        self.assertTrue(any("小节顺序" in s for s in loud["suggestions"]))

    def test_recommended_order_is_a_permutation(self):
        self.assertEqual(set(RECOMMENDED_SECTION_ORDER), set(BODY_SECTIONS))
        self.assertEqual(len(RECOMMENDED_SECTION_ORDER), len(BODY_SECTIONS))


class WiringTests(unittest.TestCase):
    """接线：两个导出页必须把顺序/外观真的传下去，不能只存在属性里。"""

    def _export_call_keywords(self, rel, func_names):
        path = os.path.join(_ROOT, "offerflow_desktop", rel)
        with open(path, encoding="utf-8") as fh:
            tree = ast.parse(fh.read())
        found = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name in func_names:
                for sub in ast.walk(node):
                    if isinstance(sub, ast.Call) and isinstance(sub.func, ast.Name):
                        if sub.func.id in ("export_pdf", "export_pdf_from_blocks",
                                           "export_docx", "export_docx_from_blocks"):
                            found[sub.func.id] = {kw.arg for kw in sub.keywords}
        return found

    def test_export_pages_pass_order_and_appearance(self):
        for rel in ("ui/pages/resume_generate.py", "ui/pages/resume_optimize.py"):
            calls = self._export_call_keywords(rel, ("_export",))
            self.assertTrue(calls, f"{rel} 没找到导出调用")
            for name, kwargs in calls.items():
                with self.subTest(file=rel, call=name):
                    self.assertIn("order", kwargs, f"{rel} 的 {name} 没传 order")
                    self.assertIn("appearance", kwargs, f"{rel} 的 {name} 没传 appearance")

    def test_pages_feed_ats_with_template_and_order(self):
        """生成页每次重算 ATS 都得带上用户**当前**选的版式与模块顺序。

        两层判据：

        1. 页面上**每一处** `revalidate(...)` 调用点都要传 `template` +
           `section_order` —— 漏传一个，用户换了版式后界面上的 ATS 分数还是按旧
           设置算的（分数与导出稿对不上）；
        2. 两条编辑路径（改排序/切版式 → `_refresh_after_edit`；换候选写法 →
           `_on_variants`）**最终都要走到**这个调用点。

        第 2 条按**调用链**判而不是数出现次数：两处调用点后来被收敛进
        `_revalidate_current()`（合并回写 `_current_result`，顺带修掉
        「换候选后丢 `id`，再点「换 3 个写法」必失败」），「至少两处」这种
        计数判据会随重构形状失效，而真正要守的「两条路径都触发重算」不会。
        """
        path = os.path.join(_ROOT, "offerflow_desktop", "ui/pages/resume_generate.py")
        with open(path, encoding="utf-8") as fh:
            tree = ast.parse(fh.read())
        by_name = {n.name: n for n in ast.walk(tree)
                   if isinstance(n, ast.FunctionDef)}

        def reachable(fn_name, seen=None):
            """从某方法出发（沿本模块方法调用）能触达的 revalidate 调用。"""
            seen = set() if seen is None else seen
            if fn_name in seen or fn_name not in by_name:
                return []
            seen.add(fn_name)
            node = by_name[fn_name]
            found = [sub for sub in ast.walk(node)
                     if isinstance(sub, ast.Call)
                     and isinstance(sub.func, ast.Attribute)
                     and sub.func.attr == "revalidate"]
            for sub in ast.walk(node):
                if isinstance(sub, ast.Call) and isinstance(sub.func, ast.Attribute):
                    found.extend(reachable(sub.func.attr, seen))
            return found

        calls = [sub for sub in ast.walk(tree)
                 if isinstance(sub, ast.Call)
                 and isinstance(sub.func, ast.Attribute)
                 and sub.func.attr == "revalidate"]
        self.assertTrue(calls, "生成页没有任何 revalidate 调用点，ATS 不会重算")
        for node in calls:
            kwargs = {kw.arg for kw in node.keywords}
            self.assertIn("template", kwargs, "revalidate 没传 template，ATS 看不到版式")
            self.assertIn("section_order", kwargs, "revalidate 没传 section_order")
        for edit_path in ("_refresh_after_edit", "_on_variants"):
            with self.subTest(edit_path=edit_path):
                self.assertTrue(reachable(edit_path),
                                f"{edit_path} 没走到 revalidate —— 改动后 ATS 不重算")

    def test_picker_exposes_selected_options(self):
        """导出对话框必须把三项设置都暴露出来，否则页面拿不到用户的选择。"""
        path = os.path.join(_ROOT, "offerflow_desktop", "ui/template_picker.py")
        with open(path, encoding="utf-8") as fh:
            tree = ast.parse(fh.read())
        assigned = {n.attr for n in ast.walk(tree)
                    if isinstance(n, ast.Attribute) and isinstance(n.ctx, ast.Store)}
        for attr in ("selected_key", "selected_appearance", "selected_order"):
            with self.subTest(attr=attr):
                self.assertIn(attr, assigned, f"TemplatePickerDialog 没有 {attr}")

    def test_preview_cache_key_includes_all_three(self):
        """缓存 key 漏项 = 改了设置还显示旧预览，这是最难发现的假一致。"""
        path = os.path.join(_ROOT, "offerflow_desktop", "ui/template_picker.py")
        with open(path, encoding="utf-8") as fh:
            tree = ast.parse(fh.read())
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "_cache_key":
                names = {n.attr for n in ast.walk(node) if isinstance(n, ast.Attribute)}
                self.assertIn("selected_key", names)
                self.assertIn("selected_appearance", names)
                self.assertIn("selected_order", names)
                return
        self.fail("TemplatePickerDialog 缺少 _cache_key")


if __name__ == "__main__":
    unittest.main(verbosity=2)
