"""JSON Resume 互转的判据：映射 / 往返 / 容错 / 反向对照 / 官方 schema 校验。

判据取向（按错题本）
--------------------
* 断言全部打在**产物**上，不打在判据自己身上；
* 反向对照把缺陷放在**产物侧**（若把锚点一起删掉，就是两边同时改 = 恒真），
  并且每条都配一句「正确产物在同一判据下必须通过」的自证；
* 报 FAIL 之前先怀疑判据：这个文件的期望值已经被写错过两次
  （技能条数按另一份样本算、日期拿两组不同路径去比）。

外部判据
--------
`tests/data/jsonresume-schema-1.0.0.json` 是官方 schema（来自 npm
``resume-schema`` 1.0.0，MIT）随仓库附带，所以 schema 校验**不依赖网络**。
它需要 ``jsonschema`` 包；项目运行环境没装时用例会**显式跳过**而不是假装通过 ——
"跳过"会在输出里露出来，"假装通过"会让整份报告失真。
"""

import json
import os
import re
import sys
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from offerflow_desktop.core import json_resume as jr              # noqa: E402
from offerflow_desktop.core import resume_review                  # noqa: E402

SCHEMA_PATH = os.path.join(_ROOT, "tests", "data", "jsonresume-schema-1.0.0.json")

# 官方 schema 里 iso8601 的 pattern（原样抄自 schema 的 definitions.iso8601）。
ISO = re.compile(r"^([1-2][0-9]{3}-[0-1][0-9]-[0-3][0-9]"
                 r"|[1-2][0-9]{3}-[0-1][0-9]|[1-2][0-9]{3})$")

# 覆盖全部映射分支：三种经历类型、一条「至今」、分组技能；教育带 detail。
SAMPLE = {
    "name": "张三",
    "position": "AI 应用研发（RAG 方向）",
    "summary": "三年 RAG 检索与文档智能经验。",
    "contact": {"phone": "13800138000", "email": "zhangsan@example.com",
                "github": "github.com/zhangsan"},
    "education": [
        {"school": "某某大学", "major": "计算机科学与技术", "degree": "本科",
         "start": "2020年9月", "end": "2024.06", "detail": "GPA 3.8/4.0"},
    ],
    "experiences": [
        {"name": "合合信息", "role": "算法实习生", "type": "internship",
         "start": "2023.07", "end": "2023.09",
         "bullets": ["搭了 RAG 检索链路", "召回率 +12%"]},
        {"name": "文档解析课程项目", "role": "组长", "type": "project",
         "start": "2022.03", "end": "2022.06", "bullets": ["写了 PDF 解析器"]},
        {"name": "某某科技", "role": "后端工程师", "type": "work",
         "start": "2024.07", "end": "至今", "bullets": ["维护检索服务"]},
    ],
    "skills": [],
    "skill_groups": {"编程语言": ["Python", "Go"], "框架": ["FastAPI"]},
    "certifications": ["CET-6", "软考中级"],
}

FLAT_SAMPLE = dict(SAMPLE, skill_groups={}, skills=["Python", "Go"])


class DateNormalizeTest(unittest.TestCase):
    """schema 只认 YYYY / YYYY-MM / YYYY-MM-DD，归一必须彻底。"""

    def test_common_spellings(self):
        for raw, want in (("2023.09", "2023-09"), ("2023年9月", "2023-09"),
                          ("2023/9", "2023-09"), ("2023-09-01", "2023-09-01"),
                          ("2023", "2023"), ("2023.9.1", "2023-09-01"),
                          ("  2023-9 ", "2023-09")):
            self.assertEqual(jr.normalize_date(raw), want, raw)

    def test_unrecognized_returns_empty_not_original(self):
        """认不出来必须返回空串 —— 返回原值会让整份文件在别人的工具里非法。"""
        for raw in ("至今", "今", "随时", "待定", "", None, "abc"):
            self.assertEqual(jr.normalize_date(raw), "", raw)

    def test_present_word_is_detected_separately(self):
        for raw in ("至今", "今", "现在", "now", "PRESENT", "Current"):
            self.assertTrue(jr._is_present_word(raw), raw)
        for raw in ("", "2024.07", "在职"):
            self.assertFalse(jr._is_present_word(raw), raw)


class ExperienceKindTest(unittest.TestCase):

    def test_known_kinds(self):
        for kind in ("work", "internship", "project", "WORK", "Internship"):
            self.assertEqual(jr.normalize_experience_kind(kind), kind.lower())

    def test_chinese_and_unknown_fall_back(self):
        self.assertEqual(jr.normalize_experience_kind("实习经历"), "internship")
        self.assertEqual(jr.normalize_experience_kind("工作经历"), "work")
        self.assertEqual(jr.normalize_experience_kind("项目经历"), "project")
        self.assertEqual(jr.normalize_experience_kind("随便什么"), jr.DEFAULT_KIND)
        self.assertEqual(jr.normalize_experience_kind(None), jr.DEFAULT_KIND)


class ExportTest(unittest.TestCase):
    """逐字段核对落点。"""

    def setUp(self):
        self.data = jr.to_json_resume(SAMPLE)

    def test_basics(self):
        basics = self.data["basics"]
        self.assertEqual(basics["name"], "张三")
        self.assertEqual(basics["label"], SAMPLE["position"])
        self.assertEqual(basics["summary"], SAMPLE["summary"])
        self.assertEqual(basics["phone"], "13800138000")
        self.assertEqual(basics["email"], "zhangsan@example.com")

    def test_github_goes_to_profiles_with_scheme(self):
        self.assertEqual(self.data["basics"]["profiles"],
                         [{"network": "GitHub", "url": "https://github.com/zhangsan"}])

    def test_experiences_split_by_kind(self):
        self.assertEqual([w["name"] for w in self.data["work"]], ["某某科技"])
        self.assertEqual([p["name"] for p in self.data["projects"]],
                         ["合合信息", "文档解析课程项目"])
        self.assertEqual([p["type"] for p in self.data["projects"]],
                         ["internship", "project"])

    def test_role_field_differs_between_work_and_projects(self):
        """work[] 用 position，projects[] 用 roles —— 标准里就是这样。"""
        self.assertEqual(self.data["work"][0]["position"], "后端工程师")
        self.assertEqual(self.data["projects"][0]["roles"], ["算法实习生"])

    def test_dates_are_normalized(self):
        self.assertEqual(self.data["education"][0]["startDate"], "2020-09")
        self.assertEqual(self.data["work"][0]["startDate"], "2024-07")

    def test_education_detail_goes_to_courses(self):
        self.assertEqual(self.data["education"][0]["courses"], ["GPA 3.8/4.0"])

    def test_skill_groups_map_naturally(self):
        self.assertEqual(self.data["skills"],
                         [{"name": "编程语言", "keywords": ["Python", "Go"]},
                          {"name": "框架", "keywords": ["FastAPI"]}])

    def test_certifications(self):
        self.assertEqual([c["name"] for c in self.data["certificates"]],
                         ["CET-6", "软考中级"])

    def test_present_is_not_written_as_end_date(self):
        """「至今」写进 endDate 会让 schema 校验失败，所以只能不写。"""
        self.assertNotIn("endDate", self.data["work"][0])

    def test_no_unknown_top_level_keys(self):
        """顶层是 additionalProperties: false，多一个字段整份文件就非法。"""
        allowed = {"$schema", "basics", "work", "volunteer", "education", "awards",
                   "certificates", "publications", "skills", "languages",
                   "interests", "references", "projects", "meta"}
        self.assertTrue(set(self.data) <= allowed, sorted(set(self.data) - allowed))

    def test_all_dates_match_iso8601(self):
        dates = [self.data["education"][0]["startDate"],
                 self.data["education"][0]["endDate"],
                 self.data["work"][0]["startDate"],
                 self.data["projects"][0]["endDate"]]
        for value in dates:
            self.assertRegex(value, ISO, value)

    def test_empty_fields_are_absent_not_empty_strings(self):
        data = jr.to_json_resume({"name": "只有名字"})
        self.assertEqual(data["basics"], {"name": "只有名字"})


class RoundTripTest(unittest.TestCase):

    def setUp(self):
        self.data = jr.to_json_resume(SAMPLE)
        self.back = jr.from_json_resume(self.data)

    def test_scalar_fields(self):
        self.assertEqual(
            (self.back["name"], self.back["position"], self.back["summary"]),
            (SAMPLE["name"], SAMPLE["position"], SAMPLE["summary"]))

    def test_contact(self):
        self.assertEqual(self.back["contact"],
                         {"phone": "13800138000", "email": "zhangsan@example.com",
                          "github": "https://github.com/zhangsan"})

    def test_interleaved_experience_order_survives(self):
        """实习 → 项目 → 工作 的交错顺序全靠 meta 保住，这是它的主要理由。"""
        self.assertEqual([(e["name"], e["type"]) for e in self.back["experiences"]],
                         [(e["name"], e["type"]) for e in SAMPLE["experiences"]])

    def test_present_restored(self):
        self.assertEqual(self.back["experiences"][2]["end"], "至今")

    def test_bullets_restored(self):
        self.assertEqual(self.back["experiences"][0]["bullets"],
                         ["搭了 RAG 检索链路", "召回率 +12%"])

    def test_skill_groups_restored(self):
        self.assertEqual(self.back["skill_groups"], SAMPLE["skill_groups"])

    def test_flat_skills_are_union_of_groups(self):
        self.assertEqual(self.back["skills"], ["Python", "Go", "FastAPI"])

    def test_education_detail_restored(self):
        self.assertEqual(self.back["education"][0]["detail"], "GPA 3.8/4.0")

    def test_certifications_restored(self):
        self.assertEqual(self.back["certifications"], SAMPLE["certifications"])

    def test_second_round_trip_is_identical(self):
        """幂等：不能每导一次就漂一点。"""
        again = jr.from_json_resume(jr.to_json_resume(self.back))
        self.assertEqual(again, self.back)


class BoundaryTest(unittest.TestCase):
    """两个模型不完全重合的地方，必须有明确的取舍。"""

    def test_flat_skills_do_not_become_a_group(self):
        """扁平技能不能包成 {"name": "技能", "keywords": ...} ——
        那样导入回来会凭空多出一个叫「技能」的分组。"""
        data = jr.to_json_resume(FLAT_SAMPLE)
        self.assertEqual(data["skills"], [{"name": "Python"}, {"name": "Go"}])
        back = jr.from_json_resume(data)
        self.assertEqual(back["skill_groups"], {})
        self.assertEqual(back["skills"], ["Python", "Go"])

    def test_blank_end_and_present_end_are_distinguishable(self):
        """两者都表现为「没有 endDate」，所以必须靠 meta 的 open 标记区分。"""
        source = {"experiences": [
            {"name": "A", "type": "work", "start": "2024.01", "end": ""},
            {"name": "B", "type": "work", "start": "2024.01", "end": "至今"}]}
        data = jr.to_json_resume(source)
        self.assertTrue(all("endDate" not in w for w in data["work"]))
        self.assertEqual([r["open"] for r in data["meta"]["offerflow"]["experiences"]],
                         [False, True])
        back = jr.from_json_resume(data)
        self.assertEqual([e["end"] for e in back["experiences"]], ["", "至今"])

    def test_alien_file_without_meta_does_not_invent_present(self):
        """别人的文件缺 endDate：**不猜**「至今」（猜错会让简历上多出不实信息）。"""
        alien = {"basics": {"name": "李四"},
                 "work": [{"name": "X 公司", "startDate": "2020-01"}]}
        back = jr.from_json_resume(alien)
        self.assertEqual(back["experiences"][0]["end"], "")
        self.assertEqual(back["experiences"][0]["type"], "work")

    def test_unmapped_sections_go_to_extra_instead_of_being_dropped(self):
        alien = {"basics": {"name": "王五"},
                 "languages": [{"language": "English", "fluency": "Native speaker"}],
                 "interests": [{"name": "Wildlife", "keywords": ["Ferrets"]}]}
        back = jr.from_json_resume(alien)
        self.assertEqual(len(back["extra"]), 2)
        self.assertTrue(any("English" in x for x in back["extra"]))
        self.assertTrue(any("Wildlife" in x for x in back["extra"]))

    def test_twitter_is_not_mistaken_for_github(self):
        alien = {"basics": {"profiles": [{"network": "Twitter",
                                          "url": "https://twitter.com/x"}]}}
        self.assertNotIn("github", jr.from_json_resume(alien)["contact"])


class ToleranceTest(unittest.TestCase):
    """UI 直接调这些函数，脏输入只能降级，不能抛。"""

    def test_from_json_resume_never_raises(self):
        for bad in (None, {}, [], {"skills": "字符串"},
                    {"education": [None, 1, {"institution": "X"}]},
                    {"work": [{"highlights": "不是列表"}]},
                    {"skills": [{"name": "X", "keywords": "不是列表"}]},
                    {"basics": "不是对象"},
                    {"meta": {"offerflow": {"experiences": [{"src": "work", "i": "x"}]}}},
                    {"meta": "不是对象"}, {"projects": [{"roles": "不是列表"}]}):
            with self.subTest(bad=bad):
                self.assertIsInstance(jr.from_json_resume(bad), dict)

    def test_to_plain_text_never_raises(self):
        for bad in (None, {}, [], {"education": "不是列表"}, {"contact": "不是对象"}):
            with self.subTest(bad=bad):
                self.assertIsInstance(jr.to_plain_text(bad), str)

    def test_loads_reports_readable_errors(self):
        for bad, needle in (("{", "第"), ("[1,2]", "对象"), ("", "空"),
                            ("null", "对象"), ("不是 json", "第")):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError) as ctx:
                    jr.loads(bad)
                self.assertIn(needle, str(ctx.exception))

    def test_loads_accepts_a_normal_object(self):
        self.assertEqual(jr.loads('{"basics": {"name": "X"}}')["basics"]["name"], "X")

    def test_probe(self):
        self.assertTrue(jr.probe({"basics": {}})[0])
        self.assertTrue(jr.probe({"work": []})[0])
        self.assertFalse(jr.probe({"foo": 1})[0])
        self.assertFalse(jr.probe("字符串")[0])
        self.assertIn("basics", jr.probe({"work": []})[1])


class PlainTextTest(unittest.TestCase):
    """导入路径的终点是一条文本，它必须能被本地区质检引擎认回来。"""

    def setUp(self):
        self.text = jr.to_plain_text(jr.from_json_resume(jr.to_json_resume(SAMPLE)))
        self.doc = resume_review.parse_resume_text(self.text)

    def test_header(self):
        self.assertEqual(self.doc["name"], "张三")
        self.assertIn("13800138000", self.doc["contact_line"])
        self.assertEqual(self.doc["position"], SAMPLE["position"])
        self.assertEqual(self.doc["summary"], SAMPLE["summary"])

    def test_education_is_exactly_one_entry(self):
        """detail 单独成行会让解析器多切出一条只有 GPA 的学历（实测踩到）。"""
        self.assertEqual(len(self.doc["education"]), 1)
        self.assertIn("某某大学", self.doc["education"][0]["left"])
        self.assertEqual(self.doc["education"][0]["period"], "2020.09 – 2024.06")

    def test_experiences_keep_order(self):
        self.assertEqual(len(self.doc["experiences"]), 3)
        self.assertEqual([e["left"] for e in self.doc["experiences"]],
                         ["合合信息（算法实习生）", "文档解析课程项目（组长）",
                          "某某科技（后端工程师）"])

    def test_bullets_attached_to_their_experience(self):
        self.assertEqual(len(self.doc["experiences"][0]["bullets"]), 2)
        self.assertEqual(self.doc["experiences"][1]["bullets"], ["写了 PDF 解析器"])

    def test_skills_and_certificates(self):
        self.assertEqual(self.doc["skills"], ["Python", "Go", "FastAPI"])
        self.assertEqual(self.doc["certifications"], ["CET-6", "软考中级"])

    def test_text_is_nonempty_and_newline_terminated(self):
        self.assertGreater(len(self.text), 100)
        self.assertTrue(self.text.endswith("\n"))


class ReverseControlTest(unittest.TestCase):
    """把缺陷放回产物侧，判据必须变红；同一条判据下正确产物必须通过。"""

    def test_dropping_order_meta_breaks_interleaving(self):
        data = jr.to_json_resume(SAMPLE)
        del data["meta"]["offerflow"]["experiences"]
        broken = jr.from_json_resume(data)
        self.assertNotEqual([e["type"] for e in broken["experiences"]],
                            [e["type"] for e in SAMPLE["experiences"]])
        # 自证：正确产物在同一判据下是相等的
        self.assertEqual([e["type"] for e in jr.from_json_resume(
            jr.to_json_resume(SAMPLE))["experiences"]],
            [e["type"] for e in SAMPLE["experiences"]])

    def test_dropping_open_flag_loses_present(self):
        source = {"experiences": [{"name": "B", "type": "work",
                                   "start": "2024.01", "end": "至今"}]}
        data = jr.to_json_resume(source)
        for record in data["meta"]["offerflow"]["experiences"]:
            record["open"] = False
        broken = jr.from_json_resume(data)
        self.assertEqual([e["end"] for e in broken["experiences"]], [""])
        # 自证
        self.assertEqual([e["end"] for e in jr.from_json_resume(
            jr.to_json_resume(source))["experiences"]], ["至今"])

    def test_wrapping_flat_skills_creates_a_phantom_group(self):
        wrapped = {"skills": [{"name": "技能", "keywords": ["Python", "Go"]}]}
        self.assertNotEqual(jr.from_json_resume(wrapped)["skill_groups"], {})

    def test_raw_dates_would_fail_the_pattern(self):
        self.assertIsNone(ISO.match("2024年7月"))
        self.assertIsNone(ISO.match("至今"))
        self.assertIsNotNone(ISO.match(jr.normalize_date("2024年7月")))

    def test_detail_on_its_own_line_splits_education(self):
        naive = self._text_with_detail_split()
        doc = resume_review.parse_resume_text(naive)
        self.assertEqual(len(doc["education"]), 2)
        # 自证：当前实现是 1 条
        good = jr.to_plain_text(jr.from_json_resume(jr.to_json_resume(SAMPLE)))
        self.assertEqual(len(resume_review.parse_resume_text(good)["education"]), 1)

    @staticmethod
    def _text_with_detail_split() -> str:
        return ("张三\n13800138000\n\n教育经历\n"
                "某某大学 计算机科学与技术 本科    2020.09 – 2024.06\n"
                "GPA 3.8/4.0\n")


class VerifyExportTest(unittest.TestCase):
    """导出后的结构自检（与 PDF 的回读自检同一个理念）。"""

    def test_good_payload_passes(self):
        result = jr.verify_export(jr.to_json_resume(SAMPLE))
        self.assertTrue(result.ok, result.problems)
        self.assertTrue(result.checked)
        self.assertFalse(result.degraded)
        self.assertIn("合法", result.summary())

    def test_unknown_top_level_key_is_caught(self):
        data = dict(jr.to_json_resume(SAMPLE), foo=1)
        result = jr.verify_export(data)
        self.assertFalse(result.ok)
        self.assertIn("foo", result.summary() + "".join(result.details()))

    def test_bad_date_is_caught(self):
        data = dict(jr.to_json_resume(SAMPLE))
        data["education"] = [{"institution": "X", "startDate": "2020年9月"}]
        result = jr.verify_export(data)
        self.assertFalse(result.ok)
        self.assertIn("2020年9月", "".join(result.details()))

    def test_wrong_skill_shape_is_caught(self):
        data = dict(jr.to_json_resume(SAMPLE), skills=["Python"])
        result = jr.verify_export(data)
        self.assertFalse(result.ok)
        self.assertIn("skills", "".join(result.details()))

    def test_dirty_input_does_not_raise(self):
        for bad in (None, {}, [], "字符串", {"work": "不是数组"}):
            with self.subTest(bad=bad):
                jr.verify_export(bad)


class ExternalSchemaTest(unittest.TestCase):
    """用**官方 schema** 校验 —— 这才是「别人的工具能不能读懂」的判据。

    不使用我们自己的 `verify_export`（那是自己验自己，不构成外部判据）。
    """

    @classmethod
    def setUpClass(cls):
        try:
            import jsonschema
        except ImportError:                                # pragma: no cover
            raise unittest.SkipTest("没装 jsonschema，外部 schema 判据跳过")
        if not os.path.isfile(SCHEMA_PATH):                # pragma: no cover
            raise unittest.SkipTest("缺少 tests/data/jsonresume-schema-1.0.0.json")
        with open(SCHEMA_PATH, encoding="utf-8") as fh:
            # format_checker 是故意的：它会真去校验 uri / email，
            # 比默认严格 —— 导出给别人的文件，格式错一点在别人的工具里就白屏。
            cls.validator = jsonschema.Draft7Validator(
                json.load(fh), format_checker=jsonschema.FormatChecker())

    def assert_valid(self, payload):
        errors = sorted(self.validator.iter_errors(payload),
                        key=lambda e: list(e.path))
        self.assertFalse(errors, [(list(e.path), e.message) for e in errors][:3])

    def test_calibration_official_sample_passes(self):
        """先校准校验器本身：官方文档那份示例必须能过，否则是判据接错了。"""
        self.assert_valid({
            "basics": {"name": "John Doe", "label": "Programmer", "image": "",
                       "email": "john@gmail.com", "phone": "(912) 555-4321",
                       "url": "https://johndoe.com", "summary": "A summary…",
                       "location": {"city": "San Francisco", "countryCode": "US"},
                       "profiles": [{"network": "Twitter", "username": "john",
                                     "url": "https://twitter.com/john"}]},
            "work": [{"name": "Company", "position": "President",
                      "startDate": "2013-01-01", "endDate": "2014-01-01",
                      "highlights": ["Started the company"]}],
            "education": [{"institution": "University", "area": "Software",
                           "studyType": "Bachelor", "startDate": "2011-01-01",
                           "endDate": "2013-01-01", "score": "4.0",
                           "courses": ["DB1101 - Basic SQL"]}],
            "skills": [{"name": "Web Development", "level": "Master",
                        "keywords": ["HTML", "CSS"]}],
            "meta": {"version": "v1.0.0"},
        })

    def test_our_output_passes(self):
        self.assert_valid(jr.to_json_resume(SAMPLE))

    def test_flat_skills_output_passes(self):
        self.assert_valid(jr.to_json_resume(FLAT_SAMPLE))

    def test_minimal_output_passes(self):
        self.assert_valid(jr.to_json_resume({"name": "只有名字"}))

    def test_reverse_control_extra_field(self):
        payload = dict(jr.to_json_resume(SAMPLE), foo=1)
        errors = list(self.validator.iter_errors(payload))
        self.assertTrue(errors)
        self.assertTrue(any("foo" in e.message for e in errors),
                        [e.message for e in errors][:2])

    def test_reverse_control_raw_date(self):
        payload = dict(jr.to_json_resume(SAMPLE))
        payload["education"] = [{"institution": "X", "startDate": "2020年9月"}]
        errors = list(self.validator.iter_errors(payload))
        self.assertTrue(errors)
        self.assertTrue(any("does not match" in e.message for e in errors),
                        [e.message for e in errors][:2])

    def test_reverse_control_skill_type(self):
        payload = dict(jr.to_json_resume(SAMPLE), skills=["Python"])
        errors = list(self.validator.iter_errors(payload))
        self.assertTrue(errors)
        self.assertTrue(any("is not of type" in e.message for e in errors),
                        [e.message for e in errors][:2])


if __name__ == "__main__":
    unittest.main(verbosity=2)
