#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""阶段 2（续）：面试 / 匹配 / 薪资三个模块**本机化**的护栏验证。

承接 #441（优化模块）—— 把服务端
``modules/interview/*``、``modules/match/*``、``modules/salary_negotiation/*``
以及对应的 ``agents/graphs/{interview_prep,mock_interview,salary_*}``
搬成桌面的同步版本，并让 UI 那几条 `db.xxx()` 调用真正落到本机。

分五层：

1. **接线层（最重要）**：`api_client` 里那批「生成 / 计算」函数的函数体必须
   **一个 `_request` 都不发**。只把 UI 改成本地调用、旧云端路径留着，是本类改造
   最典型的假完成 —— 用 AST 沿函数体逐个查，而不是靠 grep 看有没有出现
   `services.xxx`（那只能证明新代码存在，证明不了旧路径不再走）。
2. **面试会话层**：题数上限 8、已结束不再出题、会话不存在要给得出信号；
   以及**点评 / 出题失败时不能让整场面试判死或卡在中间态**。
3. **薪资层**：开口价三档（`ask > high > low`）、基准数据本机版本化、
   谈判上限 5 轮且 LLM 失败也要累加轮次（否则用户永远走不到收口）。
4. **匹配层**：本机算 + 结果落本机 + 排行「本机优先、云端兜底」。
5. **同源 / 单一事实源层**：`ui/pages/salary.py` 必须真的 `start()` 那个 worker
   （原来漏了，谈判结束的总结永远出不来）；轮数上限只能有一个真源。

⚠️ 反向对照：`tests/_mutate_ai_ism_check.py` 逐条砍掉上述机制并验证本文件变红。

运行：python tests/test_ai_local_interview_salary_match.py [TestCase.test_method]
"""

import ast
import difflib
import json
import os
import re
import sys
import unittest
from unittest import mock

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from offerflow_desktop import api_client as AC                      # noqa: E402
from offerflow_desktop import local_store                           # noqa: E402
from offerflow_desktop.core import salary_benchmark as BENCH        # noqa: E402
from offerflow_desktop.core import salary_benchmark_store as BSTORE  # noqa: E402
from offerflow_desktop.services import interview as IV              # noqa: E402
from offerflow_desktop.services import match as MT                  # noqa: E402
from offerflow_desktop.services import prompts as P                 # noqa: E402
from offerflow_desktop.services import salary_negotiation as NG     # noqa: E402

_PKG = os.path.join(_ROOT, "offerflow_desktop")
_API_PATH = os.path.join(_PKG, "api_client.py")
_SALARY_PAGE = os.path.join(_PKG, "ui", "pages", "salary.py")
_CONFIG_PATH = os.path.join(_PKG, "config.py")

#: 本机化之后**绝不允许**再发网络请求的函数（各自的替代实现见 docstring）。
#: `list_matches` 不在其中 —— 它有意保留「合并云端排行」，会发一次 GET。
_CLOUD_FREE_FUNCS = (
    "calculate_match",
    "generate_interview_questions",
    "start_interview_simulate",
    "submit_interview_answer",
    "finish_interview_simulate",
    "get_interview_summary",
    "salary_reference",
    "salary_benchmark_info",
    "salary_benchmark_refresh",
    "salary_benchmark_import",
    "start_negotiation",
    "submit_negotiation_answer",
    "get_negotiation_summary",
)

#: 「顺齐」三类口径（2026-10-10 固化）。区别对待这三类，是本轮最容易被误判的地方 ——
#: 看到 `db.upload_resume` 就以为「走云端」是错的：它在 P4 就改成了本地优先。
#:
#: **写**类：本地写必须**早于**那次上行请求（先落本地、断网也能用）。
_LOCAL_FIRST_WRITERS = (
    "upload_resume",
    "delete_resume",
    "save_article",
    "delete_article",
)

#: **读**类：必须带本地回退，否则断网后列表一片空白。
#: 值为「必须出现的本地读符号」。
_LOCAL_FALLBACK_READERS = {
    "list_articles": "_local_read",
    "list_resume_versions": "_local_chain",
    "list_matches": "_local_read",
}

#: **云端兜底**类：AI 动作只允许在「本地 llm_failed」的分支里回落云端。
_CLOUD_FALLBACK_ONLY = (
    "optimize_resume",
    "generate_resume",
)


# ============ 工具 ============

def _read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def _find_func(tree, name):
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    return None


def _called_names(node):
    """函数体（含嵌套）里被**按名字**调用的函数集合（`foo(...)`）。"""
    return {n.func.id for n in ast.walk(node)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}


def _called_roots(node):
    """函数体里被调用表达式的**根对象名**。

    `_match.calculate_match(jd, resume)` 的根名是 `_match`；`_request(...)` 的根名是
    `_request`。为什么要取根名而不是函数名：委派式调用全是属性访问
    （`_interview.create_session`），只看 `ast.Name` 会把它们全部漏掉，
    「必须委派到本机实现」那条判据就成了永远失败的空靶。
    """
    out = set()
    for n in ast.walk(node):
        if not isinstance(n, ast.Call):
            continue
        func = n.func
        while isinstance(func, ast.Attribute):
            func = func.value
        if isinstance(func, ast.Name):
            out.add(func.id)
    return out


def _called_attrs(node):
    """函数体（含嵌套）里被**按属性**调用的方法名集合（如 `self._worker.start`）。"""
    return {n.func.attr for n in ast.walk(node)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}


def _attr_full(call):
    """`local_store.save(...)` → ``"local_store.save"``；不是「名字·属性」形状则 None。"""
    f = call.func
    if isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name):
        return "%s.%s" % (f.value.id, f.attr)
    return None


def _attr_calls(node):
    """函数体里 ``obj.attr(...)`` 形式的全名集合（如 `local_store.save`）。"""
    out = set()
    for n in ast.walk(node):
        if isinstance(n, ast.Call):
            name = _attr_full(n)
            if name:
                out.add(name)
    return out


def _call_linenos(node, names, obj=None):
    """函数体里调用名为 `names` 之一的行号列表（`obj` 非空时限定根对象名）。"""
    out = []
    for n in ast.walk(node):
        if not isinstance(n, ast.Call):
            continue
        full = _attr_full(n)
        if full and full.split(".")[-1] in names and (obj is None or full.split(".")[0] == obj):
            out.append(n.lineno)
            continue
        if isinstance(n.func, ast.Name) and n.func.id in names and obj is None:
            out.append(n.lineno)
    return out


def _has_const(node, text):
    """子树里是否出现内容含 `text` 的字符串常量（用来认 `result.get("llm_failed")`）。"""
    return any(isinstance(n, ast.Constant) and isinstance(n.value, str) and text in n.value
               for n in ast.walk(node))


def _is_db_call(call, names):
    """是否形如 `db.<name>(...)` / `api.<name>(...)`。"""
    f = call.func
    return (isinstance(f, ast.Attribute) and f.attr in names
            and isinstance(f.value, ast.Name) and f.value.id in ("db", "api"))


class _FakeChat:
    """假本机模型：按提示词特征分流，并记录每次调用（供断言「确实走了本机」）。

    ⚠️ 不能用 `P.NEGOTIATION_HR_SYSTEM in system` 这类判据：HR / COACH /
    ANALYSIS 三条提示词都带 `{占位符}`，调用前会 `.format()` 过，原串已经不存在。
    所以这里统一按**不变的特征短语**分流。
    """

    def __init__(self, question="请介绍你最自豪的项目经历。", feedback="优点：结构清晰；建议：补量化。",
                 coach="你接受了对方的框架，试试用价值贡献回应。", hr="这个岗位预算有限，你看能不能降一点？",
                 summary="整体表现不错，下次先锚定再让步。", profile=None, meta=None,
                 questions=None, raise_on=None):
        self.question = question
        self.feedback = feedback
        self.coach = coach
        self.hr = hr
        self.summary = summary
        self.profile = profile if profile is not None else {
            "degree": "本科", "school_tier": "普通本科", "direction": "技术研发",
            "position_raw": "后端开发工程师", "target_city_hint": "北京",
            "work_years": 0, "highlights": ["两段大模型实习"],
        }
        self.meta = meta if meta is not None else {
            "questions": ["Python 的 GIL 是什么？"], "tags": ["Python"], "difficulty": "中等",
        }
        self.questions = questions if questions is not None else [
            {"question": "讲讲你的 RAG 项目", "category": "项目",
             "difficulty": "中等", "answer": "先给结论：用两阶段检索把召回提到 92%。"},
        ]
        self.raise_on = raise_on
        self.calls = []

    def __call__(self, system, user):
        self.calls.append((system, user))
        if self.raise_on and self.raise_on in system:
            raise RuntimeError("本地模型不可用")
        if P.SALARY_PROFILE_SYSTEM in system:
            return json.dumps(self.profile, ensure_ascii=False)
        if "面经整理助手" in system:
            return json.dumps(self.meta, ensure_ascii=False)
        if P.INTERVIEW_QUESTIONS_SYSTEM in system:
            return json.dumps({"questions": self.questions}, ensure_ascii=False)
        if P.MOCK_INTERVIEWER_SYSTEM in system:
            return self.question
        if P.MOCK_EVALUATOR_SYSTEM in system:
            return self.feedback
        if P.MOCK_SUMMARY_SYSTEM in system:
            return self.summary
        if P.MATCH_SEMANTIC_SYSTEM in system:
            pairs = re.findall(r"^(\d+)\. \[(\w+)\] ", user, re.M)
            return json.dumps({"items": [
                {"index": int(i), "status": "matched", "score": 0.9,
                 "evidence": "简历原句", "missing_skills": []} for i, _ in pairs
            ]}, ensure_ascii=False)
        if "会压价的 HR" in system or "你是公司的 HR" in system:
            return self.hr
        if "薪资谈判教练" in system and "点评" in system:
            return self.coach
        if P.NEGOTIATION_SUMMARY_SYSTEM in system or "给出总结" in system:
            return self.summary
        if "你是资深薪酬顾问" in system:
            return "【你的市场价】不错。"
        return "（未识别的提示词）"


class _StoreCase(unittest.TestCase):
    """把 `local_store` 切到测试专用账号，读写都真实发生。

    ⚠️ 这条基类是**必需的**，不是可选项：断言「结果落进了本机」的用例，
    如果不先让那条写路径真的可用（`local_store` 未登录时 `save/restore` 直接
    返回 None/False），测到的只是「没登录」，而不是「没写」—— 典型的假绿。
    """

    ACCOUNT = "ism-test@local"

    def setUp(self):
        self._prev = local_store.get_account()
        local_store.set_account(self.ACCOUNT)
        local_store.clear(self.ACCOUNT)

        def _teardown():
            local_store.clear(self.ACCOUNT)
            local_store.set_account(self._prev)

        self.addCleanup(_teardown)


_RESUME = {
    "id": "r-0001", "raw_text": "张三\n某大学 计算机\n- 负责订单系统，QPS 3000",
    "parsed_json": {"skills": ["Python", "FastAPI"], "position": "后端开发"},
    "source_file": "r.docx", "source": "imported", "version": 1,
}
_JD = {
    "id": "jd-0001", "company": "某某科技", "position": "后端开发工程师",
    "must_have": ["Python", "FastAPI"], "nice_to_have": ["Redis"],
    "tech_stack": {"语言": ["Python"]}, "hidden_signals": [],
    "raw_text": "招聘后端开发工程师，要求熟悉 Python、FastAPI、Redis。",
}


# ============ 1. 接线层：这批函数体里不许再出现云端请求 ============

class TestNoCloudAnywhere(unittest.TestCase):
    """核心判据：本机化必须**真的落地**，不是「新写一份、旧的照走」。"""

    def test_cloud_free_functions_never_call_request(self):
        tree = ast.parse(_read(_API_PATH))
        offenders = {}
        for name in _CLOUD_FREE_FUNCS:
            fn = _find_func(tree, name)
            self.assertIsNotNone(fn, "api_client 里找不到 %s" % name)
            if "_request" in _called_names(fn):
                offenders[name] = "仍在调用 _request()"
        self.assertEqual(offenders, {}, "以下函数没有真正本机化：%s" % offenders)

    def test_cloud_free_functions_do_delegate_to_services(self):
        """反向保护：不能靠「把函数体清空」骗过上面那条。"""
        tree = ast.parse(_read(_API_PATH))
        expect = {
            "calculate_match": "_match",
            "generate_interview_questions": "_interview",
            "start_interview_simulate": "_interview",
            "salary_reference": "_nego",
            "start_negotiation": "_nego",
            "salary_benchmark_info": "_bench_store",
            "salary_benchmark_import": "_bench_store",
        }
        srcs = {name: _called_roots(_find_func(tree, name)) for name in expect}
        missing = ["%s→%s" % (n, mod) for n, mod in expect.items() if mod not in srcs[n]]
        self.assertEqual(missing, [], "这些函数没有委派到本机实现：%s" % missing)

    def test_services_modules_do_not_import_api_client(self):
        """本机服务层不能反向依赖云端客户端（否则「本地优先」就成了循环）。"""
        for mod in ("interview.py", "match.py", "salary_negotiation.py"):
            tree = ast.parse(_read(os.path.join(_PKG, "services", mod)))
            imported = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    imported |= {a.name for a in node.names}
                elif isinstance(node, ast.ImportFrom):
                    imported |= {"%s.%s" % (node.module or "", a.name) for a in node.names}
            bad = sorted(n for n in imported if "api_client" in n)
            self.assertEqual(bad, [], "%s 反向导入了 api_client：%s" % (mod, bad))

    # ---- 三类口径：本地优先写 / 本地回退读 / 云端只兜底 ----

    def test_local_first_writers_touch_local_store_before_request(self):
        """本地优先的「写」函数：本机写入必须**早于**那次上行请求。

        只断言「函数体里出现过 local_store」是空靶 —— 把 `_request` 提到最前面、
        本地写挪到最后，「先联网、失败了再写本地」的实现照样能过。所以必须比行号。
        """
        tree = ast.parse(_read(_API_PATH))
        writes = {"local_store.save", "local_store.soft_delete",
                  "local_store.restore", "local_store.get_item"}
        offenders = {}
        for name in _LOCAL_FIRST_WRITERS:
            fn = _find_func(tree, name)
            self.assertIsNotNone(fn, "api_client 里找不到 %s" % name)
            self.assertTrue(_attr_calls(fn) & writes, "%s 完全没有本机写入" % name)
            l_local = min(n.lineno for n in ast.walk(fn)
                          if isinstance(n, ast.Call) and _attr_full(n) in writes)
            reqs = _call_linenos(fn, {"_request"})
            if reqs and l_local > min(reqs):
                offenders[name] = "本机写入(L%d) 晚于 _request(L%d)" % (l_local, min(reqs))
        self.assertEqual(offenders, {},
                         "这些函数把上行放在了本机写入之前：%s" % offenders)

    def test_local_fallback_readers_merge_local_data(self):
        """本地回退的「读」函数：函数体里必须出现本地读。

        纯云端的 `return _as_list(_request(...))` 在这条会变红 —— 它正是
        「断网后面经列表 / 版本链一片空白」的根因。
        """
        tree = ast.parse(_read(_API_PATH))
        missing = []
        for name, expect in _LOCAL_FALLBACK_READERS.items():
            fn = _find_func(tree, name)
            self.assertIsNotNone(fn, "api_client 里找不到 %s" % name)
            if expect not in (_called_names(fn) | _attr_calls(fn)):
                missing.append("%s 缺 %s" % (name, expect))
        self.assertEqual(missing, [], "读函数没有本地回退：%s" % missing)

    def test_cloud_fallback_only_reached_under_llm_failed(self):
        """云端兜底（optimize / generate）只允许出现在「本地 llm_failed」的分支里。

        判据是**结构性的**：UI 里每一处 `db.optimize_resume(...)` /
        `db.generate_resume(...)` 都必须位于一个 test 含 `'llm_failed'` 的 `if`
        语句体内。「函数里出现过 llm_failed 这个词」不算 —— 一次无条件调用也能过。
        """
        targets = set(_CLOUD_FALLBACK_ONLY)
        offenders = []
        pages = os.path.join(_PKG, "ui", "pages")
        for fname in sorted(os.listdir(pages)):
            if not fname.endswith(".py"):
                continue
            tree = ast.parse(_read(os.path.join(pages, fname)))
            guarded = set()
            for n in ast.walk(tree):
                if isinstance(n, ast.If) and _has_const(n.test, "llm_failed"):
                    for st in n.body:
                        for c in ast.walk(st):
                            if isinstance(c, ast.Call) and _is_db_call(c, targets):
                                guarded.add(c.lineno)
            for n in ast.walk(tree):
                if isinstance(n, ast.Call) and _is_db_call(n, targets):
                    if n.lineno not in guarded:
                        offenders.append("%s:%d %s" % (fname, n.lineno, n.func.attr))
        self.assertEqual(offenders, [],
                         "这些云端兜底调用不在 llm_failed 分支里：%s" % offenders)


class TestLocalFirstDataPaths(_StoreCase):
    """「本地优先」不能只靠 docstring 相信 —— 用真实离线注入证明。

    这三处曾被**误判**成「还在走云端」（只看了 `db.xxx` 的字面，没读函数体）：
    `upload_resume` / `list_resume_versions` / `delete_resume`。把 `_request`
    一律打到断网，它们必须照常可用 —— 这才是「服务器只当数据库」的实证。
    """

    _TEXT = "张三\n某大学 计算机科学与技术\n- 负责订单系统，QPS 从 3000 提升到 9000\n"

    def _upload_offline(self):
        with mock.patch.object(AC, "_request", side_effect=AC.ApiOfflineError("断网")):
            return AC.upload_resume(self._TEXT, "resume.docx")

    def test_upload_resume_works_offline(self):
        out = self._upload_offline()
        self.assertTrue(str(out.get("id") or ""), "离线导入没拿到本地 id")
        self.assertEqual(str(out.get("source_file") or ""), "resume.docx")

    def test_list_resume_versions_sees_local_chain_offline(self):
        """断网时版本链必须能看到刚导入的那一版 —— 否则用户以为没保存。"""
        rid = str(self._upload_offline().get("id"))
        with mock.patch.object(AC, "_request", side_effect=AC.ApiOfflineError("断网")):
            rows = AC.list_resume_versions(rid)
        self.assertTrue(rows, "断网时版本链是空的")
        self.assertEqual(str(rows[0].get("id") or ""), rid)

    def test_delete_resume_works_offline(self):
        """离线删除必须不抛 —— 本地先删、排队等同步（服务器上那份联网后再删）。"""
        rid = str(self._upload_offline().get("id"))
        with mock.patch.object(AC, "_request", side_effect=AC.ApiOfflineError("断网")):
            AC.delete_resume(rid)


# ============ 2. 面试会话层 ============

class TestInterviewSession(unittest.TestCase):

    def setUp(self):
        IV.reset_sessions()
        self.addCleanup(IV.reset_sessions)
        self.chat = _FakeChat()

    def _start(self):
        out = IV.create_session("r-1", "jd-1", "张三\n某大学", "招聘后端",
                               chat_fn=self.chat)
        self.assertEqual(out["question_number"], 1)
        return out["session_id"]

    def test_finishes_exactly_at_max_questions(self):
        """题数上限 8：第 8 次提交收口、且不再出第 9 题。

        判据必须看**边界那一次**的返回值（上限写成 7 或 9 时，只看「最终能结束」
        都会通过）；轮数用字面量 8 而不是 `IV.MAX_QUESTIONS` —— 后者会让用例
        跟着常量走，常量改错时用例跟着一起错。
        """
        self.assertEqual(IV.MAX_QUESTIONS, 8, "与服务端 8 题上限不同值")
        sid = self._start()
        for i in range(7):
            res = IV.submit_answer(sid, "答案%d" % i, chat_fn=self.chat)
            self.assertFalse(res["is_finished"], "第 %d 轮不该结束" % (i + 1))
            self.assertIsNotNone(res["next_question"])
        last = IV.submit_answer(sid, "第 8 答", chat_fn=self.chat)
        self.assertTrue(last["is_finished"], "到第 8 题必须收口")
        self.assertIsNone(last["next_question"], "收口后不该再出题")
        session = IV.get_session(sid)
        self.assertEqual(session["status"], "finished")
        self.assertEqual(len(session["questions"]), 8)

    def test_answer_after_finish_is_rejected_not_silently_continued(self):
        sid = self._start()
        IV.finish_session(sid)
        res = IV.submit_answer(sid, "再答一次", chat_fn=self.chat)
        self.assertTrue(res["is_finished"])
        self.assertIsNone(res["next_question"])
        self.assertIsNone(res["feedback"], "已结束的会话不该再给点评")

    def test_unknown_session_reports_none_feedback(self):
        """会话不存在必须给出**可区分**的信号（`feedback is None`），供上层报错。"""
        res = IV.submit_answer("local-sim-nope", "x", chat_fn=self.chat)
        self.assertIsNone(res["feedback"])
        self.assertFalse(res["is_finished"])

    def test_coach_failure_does_not_kill_the_session(self):
        """本轮点评失败 → 给一句说明文案，**不能**回落成 None（那会被当成会话不存在）。"""
        sid = self._start()
        bad = _FakeChat(raise_on="MOCK_EVALUATOR")
        with mock.patch.object(IV, "evaluate_answer", side_effect=RuntimeError("模型挂了")):
            res = IV.submit_answer(sid, "我的回答", chat_fn=bad)
        self.assertIsNotNone(res["feedback"])
        self.assertIn("点评", res["feedback"])
        self.assertFalse(res["is_finished"], "点评失败不该把整场面试判死")
        self.assertIsNotNone(res["next_question"])

    def test_question_failure_closes_cleanly(self):
        """出下一题失败 → 这一轮就收口，不能停在「已答完但出不了题」的中间态。"""
        sid = self._start()
        with mock.patch.object(IV, "generate_question", side_effect=RuntimeError("模型挂了")):
            res = IV.submit_answer(sid, "我的回答", chat_fn=self.chat)
        self.assertTrue(res["is_finished"], "出不了下一题时必须收口")
        self.assertIsNone(res["next_question"])
        self.assertEqual(IV.get_session(sid)["status"], "finished")

    def test_empty_resume_is_rejected(self):
        with self.assertRaises(ValueError):
            IV.create_session("r", "j", "   ", "JD", chat_fn=self.chat)

    def test_question_alias_tables_do_not_drift_from_canonical_values(self):
        """防漂移护栏：规范值必须自映射，别名表不许指向不存在的规范值。

        与薪资侧「方向 key 必须能在系数表查到」同款护栏 —— 改了常量忘了改别名表
        会让某一类题被静默丢弃，而且没有任何报错。
        """
        for c in IV.QUESTION_CATEGORIES:
            self.assertEqual(IV.QUESTION_CATEGORY_ALIASES.get(c), c)
        for d in IV.QUESTION_DIFFICULTIES:
            self.assertEqual(IV.QUESTION_DIFFICULTY_ALIASES.get(d), d)
        for table, canon in ((IV.QUESTION_CATEGORY_ALIASES, IV.QUESTION_CATEGORIES),
                             (IV.QUESTION_DIFFICULTY_ALIASES, IV.QUESTION_DIFFICULTIES)):
            self.assertTrue(set(table.values()) <= set(canon),
                            "别名指向了不存在的规范值：%s" % (set(table.values()) - set(canon)))

    def test_question_format_normalizes_aliases_and_keeps_real_questions(self):
        """#6：分类 / 难度别名必须归一，不能因为「技术题」把整条题丢掉。

        旧版是严格白名单，模型写「技术题」「technical」「一般」「medium」，
        整条题被判无效；而 `generate_interview_questions` 只把「没有合法题」变成空列表，
        界面于是说「未生成题目，请重试」—— 用户完全不知道是**格式**被卡掉了。
        """
        self.assertTrue(IV._validate_question(
            {"question": "Q", "category": "技术", "difficulty": "简单"}))

        cases = [
            ({"category": "技术题", "difficulty": "简单"}, ("技术", "简单")),
            ({"category": "technical", "difficulty": "easy"}, ("技术", "简单")),
            ({"category": "项目经验", "difficulty": "一般"}, ("项目", "中等")),
            ({"category": "behavioral", "difficulty": "medium"}, ("行为", "中等")),
            ({"category": "综合", "difficulty": "高难"}, ("行为", "困难")),
            ({"category": " 项目 ", "difficulty": " Medium "}, ("项目", "中等")),
        ]
        for extra, expect in cases:
            norm = IV.normalize_question(dict({"question": "Q"}, **extra))
            self.assertIsNotNone(norm, "别名被误杀：%r" % (extra,))
            self.assertEqual((norm["category"], norm["difficulty"]), expect)

        # 真·不合法仍要拒（"宽松"不能变成"什么都放行"）
        for bad in ({"question": "Q", "category": "数学", "difficulty": "简单"},
                    {"question": "Q", "category": "技术", "difficulty": "地狱"},
                    {"question": "   ", "category": "技术", "difficulty": "简单"},
                    {"category": "技术", "difficulty": "简单"},
                    "不是 dict",
                    None):
            self.assertFalse(IV._validate_question(bad), "不该放行：%r" % (bad,))

    def test_summary_shape_matches_web(self):
        sid = self._start()
        IV.submit_answer(sid, "答", chat_fn=self.chat)
        out = IV.summary_of(IV.get_session(sid), chat_fn=self.chat)
        for key in ("summary", "total_questions", "questions", "answers",
                    "feedbacks", "status"):
            self.assertIn(key, out)
        self.assertEqual(out["total_questions"], len(out["questions"]))
        self.assertEqual(len(out["answers"]), len(out["feedbacks"]))

    def test_model_call_runs_outside_sessions_lock(self):
        """点评/出题必须在全局锁**之外**。

        旧版 `submit_answer` 用 `with _SESSIONS_LOCK` 把点评 + 出题整段裹住，
        一次 20~30s 的模型调用会把 `get_session` / `finish_session` 全堵死
        （界面「结束面试 → 取总结」只能干等）。

        判据：在模型回调里尝试获取 `_SESSIONS_LOCK`。同一线程对**非重入** Lock
        再次 acquire 会阻塞到超时，所以「调用时持锁」⇒ 判据必为 False。
        """
        sid = self._start()
        probe = {"calls": 0, "free": True}

        def chat_fn(system, user):
            # 探针必须**粘性**：submit_answer 会调 chat_fn 两次（点评 + 出题），
            # 只记最后一次会被「锁外那一次」覆盖成 True —— 反向对照实测抓到过这个空靶。
            got = IV._SESSIONS_LOCK.acquire(timeout=1.0)
            if got:
                IV._SESSIONS_LOCK.release()
            probe["calls"] += 1
            probe["free"] = probe["free"] and got
            return self.chat(system, user)

        IV.submit_answer(sid, "我的回答", chat_fn=chat_fn)
        self.assertGreaterEqual(probe["calls"], 2, "两次模型回调都要跑到（判据非空靶）")
        self.assertTrue(probe["free"], "模型调用期间仍持有 _SESSIONS_LOCK")

    # ---- #7 全场题单 + 出题去重 ----

    def test_question_prompt_carries_every_asked_question(self):
        """#7：出下一题必须把**整场**问过的题交给模型，而不是只给上一问上一答。

        旧版只喂上一问上一答，模型看不到前面问过什么，第 5 题很容易把第 2 题的考点
        重问一遍（用户会觉得 AI 没在听）。
        """
        seq = ["第一题：做一下自我介绍", "第二题：讲讲项目里的最大难点",
               "第三题：讲一次你处理冲突的经历"]
        prompts = []
        idx = {"n": 0}

        def chat_fn(system, user):
            if P.MOCK_INTERVIEWER_SYSTEM in system:
                prompts.append(user)
                q = seq[min(idx["n"], len(seq) - 1)]
                idx["n"] += 1
                return q
            return self.chat(system, user)

        sid = IV.create_session("r-1", "jd-1", "张三\n某大学", "招聘后端",
                                chat_fn=chat_fn)["session_id"]
        IV.submit_answer(sid, "答一", chat_fn=chat_fn)
        IV.submit_answer(sid, "答二", chat_fn=chat_fn)

        self.assertEqual(len(prompts), 3, "三次出题应各调一次模型")
        # 第一题时还没有"已问题单"，不该出现该段
        self.assertNotIn("已经问过的题目", prompts[0])
        # 第二次出题（第三题）时必须带上前两题
        self.assertIn("已经问过的题目", prompts[2])
        self.assertIn(seq[0], prompts[2], "整场题单漏了第一题")
        self.assertIn(seq[1], prompts[2], "整场题单漏了第二题")

    def test_duplicate_question_triggers_one_retry(self):
        """#7：模型重复出题时要重试一次，而不是把重复的题原样塞给用户。"""
        answers = ["重复考点：讲讲你的项目", "重复考点：讲讲你的项目", "换个考点：说说你的取舍"]
        idx = {"n": 0}

        def chat_fn(system, user):
            if P.MOCK_INTERVIEWER_SYSTEM in system:
                q = answers[min(idx["n"], len(answers) - 1)]
                idx["n"] += 1
                return q
            return self.chat(system, user)

        sid = IV.create_session("r-1", "jd-1", "张三", "后端", chat_fn=chat_fn)["session_id"]
        res = IV.submit_answer(sid, "我的回答", chat_fn=chat_fn)
        self.assertEqual(idx["n"], 3, "重复出题没有触发重试（只调了一次模型）")
        self.assertEqual(res["next_question"], "换个考点：说说你的取舍")
        self.assertNotEqual(res["next_question"], answers[0])

    def test_question_dedup_ignores_punctuation_and_spacing(self):
        """去重判据本身：标点 / 空白差异不算新题。"""
        self.assertTrue(IV.is_duplicate_question(
            "你为什么选择我们公司？", ["你为什么选择我们公司"]))
        self.assertFalse(IV.is_duplicate_question(
            "你如何看待加班", ["你为什么选择我们公司"]))

    def test_build_report_counts_dropped_questions(self):
        """#6：诊断信息必须真实 —— raw_count 是模型给的条数，dropped 是格式被丢弃的条数。"""
        fake = _FakeChat(questions=[
            {"question": "Q1", "category": "技术题", "difficulty": "一般", "answer": "答"},
            {"question": "Q2", "category": "数学", "difficulty": "简单", "answer": "答"},
            {"question": "Q3", "category": "技术", "difficulty": "地狱", "answer": "答"},
        ])
        rep = IV.build_interview_questions("张三", "招聘后端", limit=5, chat_fn=fake)
        self.assertEqual(rep["raw_count"], 3)
        self.assertEqual(rep["dropped"], 2, "被丢弃的条数没数对")
        self.assertEqual(len(rep["questions"]), 1, "别名那条应该被留下")
        self.assertEqual(rep["questions"][0]["category"], "技术")
        self.assertEqual(rep["questions"][0]["difficulty"], "中等")


class TestInterviewApiLocal(_StoreCase):

    def test_generate_questions_uses_local_model(self):
        fake = _FakeChat(questions=[
            {"question": "Q1", "category": "技术", "difficulty": "简单", "answer": "提效 40%"},
        ])
        with mock.patch.object(AC, "get_resume", return_value=dict(_RESUME)), \
             mock.patch.object(AC, "get_jd", return_value=dict(_JD)), \
             mock.patch.object(AC, "_request") as req, \
             mock.patch.object(IV, "chat", fake), \
             mock.patch.object(IV, "_load_interview_reference", return_value=""):
            out = AC.generate_interview_questions("r-0001", "jd-0001", None, 5)
        req.assert_not_called()
        self.assertEqual(len(out), 1)
        # 数字保真：答案里的「40%」简历中没有 → 必须被标出来
        self.assertEqual(out[0]["suspicious_numbers"], ["40%"])

    def test_alias_categories_are_kept_not_dropped(self):
        """#6：别名分类/难度要归一后保留，不能整条丢掉。"""
        fake = _FakeChat(questions=[
            {"question": "Q1", "category": "技术题", "difficulty": "一般", "answer": "答"},
            {"question": "Q2", "category": "behavioral", "difficulty": "easy", "answer": "答"},
        ])
        with mock.patch.object(AC, "get_resume", return_value=dict(_RESUME)), \
             mock.patch.object(AC, "get_jd", return_value=dict(_JD)), \
             mock.patch.object(IV, "chat", fake), \
             mock.patch.object(IV, "_load_interview_reference", return_value=""):
            out = AC.generate_interview_questions("r-0001", "jd-0001", None, 5)
        self.assertEqual([q["category"] for q in out], ["技术", "行为"])
        self.assertEqual([q["difficulty"] for q in out], ["中等", "简单"])

    def test_all_dropped_questions_report_format_error(self):
        """#6：模型给了题但格式**全不合法** ⇒ 报格式问题，不能只说「未生成题目」。"""
        fake = _FakeChat(questions=[
            {"question": "Q1", "category": "数学", "difficulty": "地狱"},
            {"question": "Q2", "category": "玄学", "difficulty": "简单"},
        ])
        with mock.patch.object(AC, "get_resume", return_value=dict(_RESUME)), \
             mock.patch.object(AC, "get_jd", return_value=dict(_JD)), \
             mock.patch.object(IV, "chat", fake), \
             mock.patch.object(IV, "_load_interview_reference", return_value=""):
            with self.assertRaises(AC.ApiError) as ctx:
                AC.generate_interview_questions("r-0001", "jd-0001", None, 5)
        self.assertIn("2", str(ctx.exception), "要说出被丢弃的条数")
        self.assertIn("丢弃", str(ctx.exception))

    def test_model_giving_nothing_returns_empty_list(self):
        """反向对照：模型**压根没给题**是另一回事，不能报成格式错误。"""
        fake = _FakeChat(questions=[])
        with mock.patch.object(AC, "get_resume", return_value=dict(_RESUME)), \
             mock.patch.object(AC, "get_jd", return_value=dict(_JD)), \
             mock.patch.object(IV, "chat", fake), \
             mock.patch.object(IV, "_load_interview_reference", return_value=""):
            out = AC.generate_interview_questions("r-0001", "jd-0001", None, 5)
        self.assertEqual(out, [])

    def test_empty_resume_rejected(self):
        with mock.patch.object(AC, "get_resume", return_value={"id": "r", "raw_text": " "}), \
             mock.patch.object(AC, "get_jd", return_value=dict(_JD)):
            with self.assertRaises(AC.ApiError) as ctx:
                AC.generate_interview_questions("r", "jd-0001")
        self.assertIn("简历", str(ctx.exception))

    def test_simulate_roundtrip_stays_local(self):
        fake = _FakeChat()
        with mock.patch.object(AC, "get_resume", return_value=dict(_RESUME)), \
             mock.patch.object(AC, "get_jd", return_value=dict(_JD)), \
             mock.patch.object(AC, "_request") as req, \
             mock.patch.object(IV, "chat", fake):
            started = AC.start_interview_simulate("r-0001", "jd-0001")
            self.assertTrue(started["session_id"])
            self.assertTrue(started["question"])
            stepped = AC.submit_interview_answer(started["session_id"], "我的回答")
            self.assertTrue(stepped["feedback"])
            self.assertIsNotNone(stepped["next_question"])
            AC.finish_interview_simulate(started["session_id"])
            summary = AC.get_interview_summary(started["session_id"])
        req.assert_not_called()
        self.assertIn("summary", summary)
        self.assertEqual(IV.get_session(started["session_id"])["status"], "finished")

    def test_unknown_session_raises(self):
        with self.assertRaises(AC.ApiError):
            AC.submit_interview_answer("local-sim-nope", "x")
        with self.assertRaises(AC.ApiError):
            AC.get_interview_summary("local-sim-nope")
        with self.assertRaises(AC.ApiError):
            AC.finish_interview_simulate("local-sim-nope")


class TestArticleOfflineExtraction(_StoreCase):
    """离线导入面经时必须本机抽题 —— 否则断网导入只能看到一条光秃秃的原文。"""

    def test_offline_import_extracts_questions_locally(self):
        fake = _FakeChat(meta={"questions": ["GIL 是什么？"], "tags": ["Python"],
                               "difficulty": "中等"})
        with mock.patch.object(IV, "chat", fake), \
             mock.patch.object(AC, "_request", side_effect=AC.ApiOfflineError("断网")):
            out = AC.save_article("某公司", "面试官问了 GIL 和 GIL 释放机制……")
        self.assertTrue(out.get("queued"))
        self.assertEqual(out.get("questions"), ["GIL 是什么？"])
        self.assertEqual(out.get("difficulty"), "中等")
        self.assertFalse(out.get("metadata_pending"))

    def test_offline_import_keeps_going_when_model_unavailable(self):
        """抽题失败不能把导入整条废掉：原文照存，标成待同步。"""
        with mock.patch.object(AC, "_request", side_effect=AC.ApiOfflineError("断网")), \
             mock.patch.object(IV, "chat", _FakeChat(raise_on="面经整理助手")):
            out = AC.save_article("某公司", "面试官问了 GIL 和 GIL 释放机制……")
        self.assertTrue(out.get("queued"))
        self.assertEqual(out.get("questions"), [])
        self.assertTrue(out.get("metadata_pending"),
                        "抽题失败却没标记待同步 —— 联网后不会被补齐")

    def test_offline_duplicate_is_detected_locally(self):
        """离线也要去重，否则断网反复导入同一份面经会在本地堆出一串副本。"""
        text = "面试官先问项目，再问 GIL，最后让我手写一个 LRU 缓存。"
        with mock.patch.object(AC, "_request", side_effect=AC.ApiOfflineError("断网")), \
             mock.patch.object(IV, "chat", _FakeChat()):
            first = AC.save_article("某公司", text)
            second = AC.save_article("某公司", text)
        self.assertFalse(first.get("duplicate"))
        self.assertTrue(second.get("duplicate"), "同一份面经第二次导入必须判重")
        self.assertEqual(len(local_store.list_items("articles")), 1)

    def test_duplicate_helper_returns_the_hit_article(self):
        arts = [{"id": "a1", "raw_content": "短"}, {"id": "a2", "raw_content": "面试官先问项目再问 GIL"}]
        hit = IV.find_duplicate_article("面试官先问项目，再问 GIL", arts)
        self.assertIsNotNone(hit)
        self.assertEqual(hit["id"], "a2")


class TestAnswerFidelityNumbers(unittest.TestCase):
    """#8：数字保真必须**连单位一起看**，同时别把常见表述误报。"""

    def test_same_number_with_conflicting_time_unit_is_flagged(self):
        """旧版把单位剥掉只比数字：「3 年」成了「3 个月」的出处，一处编造被放行。"""
        out = IV.check_answer_fidelity("3 年 Python 开发经验", "我带过 3 个月的实习团队")
        self.assertIn("3 个月", out)

    def test_same_number_same_time_unit_is_fine(self):
        self.assertEqual(IV.check_answer_fidelity("3 年 Python 开发经验", "有 3 年的开发经验"), [])

    def test_number_absent_from_resume_is_still_flagged(self):
        self.assertEqual(IV.check_answer_fidelity("负责订单系统", "提效 40%"), ["40%"])

    def test_chinese_numeral_in_resume_covers_arabic_in_answer(self):
        """「两段实习」与「2 段实习」是同一处事实，不该误报（常见误报来源）。"""
        self.assertEqual(IV.check_answer_fidelity("有两段大模型实习", "完成了 2 段实习"), [])
        self.assertEqual(IV.check_answer_fidelity("三年后端经验", "3 年后端经验"), [])

    def test_unit_difference_on_non_time_unit_is_not_flagged(self):
        """反向对照：非时间单位的差异不该报（避免把判据做成"凡单位不同就报"）。"""
        self.assertEqual(IV.check_answer_fidelity("QPS 3000", "峰值做到了 3000 QPS"), [])
        self.assertEqual(IV.check_answer_fidelity("服务 200 人", "服务 200 个用户"), [])

    def test_unitless_mention_in_resume_does_not_pretend_to_be_a_time_unit(self):
        """简历里 3 没有单位 ⇒ 不能把答案的「3 个月」判成"和简历对不上"。"""
        self.assertEqual(IV.check_answer_fidelity("参与 3 个模块", "做了 3 个月"), [])


class TestArticleDedupNormalization(unittest.TestCase):
    """#11：面经查重先做标点/全半角归一，再做「整段 + 分段指纹」两路判据。"""

    def test_punctuation_and_fullwidth_differences_still_dup(self):
        """⚠️ 用例里的标点必须挑 **NFKC 不会转换** 的那批（。、），否则证明不了
        「去标点」这一步：全半角逗号/分号在 NFKC 下本来就等价（反向对照抓到过这条空靶）。"""
        a = "面试官先问项目。再问 GIL、最后让我手写 LRU 缓存。"
        b = "面试官先问项目，再问 GIL；最后让我手写 LRU 缓存"
        self.assertTrue(IV.is_duplicate_article(a, [{"id": "x", "raw_content": b}]))

    def test_dedup_normalize_strips_punctuation_not_just_whitespace(self):
        """直接测归一化本身：只去空白不够 —— NFKC 只能处理全半角那一部分标点。"""
        a = IV._dedup_normalize("面试官先问项目。再问 GIL、最后让我手写 LRU 缓存")
        b = IV._dedup_normalize("面试官先问项目, 再问 GIL, 最后让我手写 LRU 缓存")
        self.assertEqual(a, b, "标点没被去掉：换个标点就能绕过查重")

    def test_long_article_with_added_paragraph_still_dup(self):
        """补了几段再导入也要判重。

        整段相似度会掉到 0.9 以下（424/524 ≈ 0.81），必须靠**分段指纹**兜住 ——
        这是「同一份面经重导」最常见的形态。
        """
        base = "".join("第%d步：面试官追问了%s。" % (i, "细节" * 20) for i in range(1, 9))
        extended = base + "补记：三面是总监面，聊了职业规划与期望薪资。" * 5
        norm_a = IV._dedup_normalize(base)
        norm_b = IV._dedup_normalize(extended)
        self.assertLess(
            difflib.SequenceMatcher(None, norm_a, norm_b).ratio(), 0.9,
            "整段相似度必须已经掉到阈值以下，否则本用例证明不了分段指纹的作用")
        self.assertTrue(IV.is_duplicate_article(extended, [{"id": "x", "raw_content": base}]))

    def test_unrelated_article_is_not_dup(self):
        self.assertFalse(IV.is_duplicate_article(
            "今天天气不错，聊了聊公司食堂的菜",
            [{"id": "x", "raw_content": "面试官先问项目，再问 GIL"}]))

    def test_find_duplicate_returns_the_hit(self):
        hit = IV.find_duplicate_article(
            "面试官先问项目（二面），再问 GIL",
            [{"id": "a1", "raw_content": "随便写点别的"},
             {"id": "a2", "raw_content": "面试官先问项目, 再问 GIL"}])
        self.assertIsNotNone(hit)
        self.assertEqual(hit["id"], "a2")


# ============ 3. 薪资层 ============

class TestSalaryBenchmarkRules(unittest.TestCase):

    def test_ask_is_highest_and_above_range_top(self):
        """开口价必须**严格高于**区间上限，否则 HR 一压就跌破真实期望。"""
        for degree in ("associate", "bachelor_normal", "bachelor_elite", "master", "phd"):
            low, high = BENCH.estimate_salary_range(degree, "tech", "tier1")
            ask, target, bottom = BENCH.suggest_target_and_bottom(degree, "tech", "tier1")
            self.assertEqual((target, bottom), (high, low))
            self.assertGreater(ask, high, "开口价没有高于区间上限：%s" % degree)
            self.assertEqual(ask % 500, 0, "开口价没按 500 取整")

    def test_active_data_overrides_builtin_tables(self):
        """传了 `data=` 就必须按它算 —— 否则「导入了权威数据但不生效」。

        判据必须是「**两份不同的 data → 两个不同的结果**」。只断言「结果不等于内置
        原始值」是空靶：内置值经方向 / 城市系数换算后本来就不等于原始值，
        一个完全无视 `data` 的实现照样能过（反向对照当场抓到过这条假绿）。
        """
        low_table = BENCH.default_data()
        high_table = BENCH.default_data()
        low_table["degree_base"]["bachelor_normal"] = [6000, 9000]
        high_table["degree_base"]["bachelor_normal"] = [20000, 30000]

        low_a, high_a = BENCH.estimate_salary_range("bachelor_normal", "tech", "tier1",
                                                    data=low_table)
        low_b, high_b = BENCH.estimate_salary_range("bachelor_normal", "tech", "tier1",
                                                    data=high_table)
        self.assertNotEqual((low_a, high_a), (low_b, high_b), "算价完全无视了传入的生效数据")
        self.assertGreater(low_b, low_a)

        # 缺省路径不能被破坏：不传 data ≡ 传内置默认数据
        builtin = BENCH.estimate_salary_range("bachelor_normal", "tech", "tier1")
        self.assertEqual(builtin, BENCH.estimate_salary_range(
            "bachelor_normal", "tech", "tier1", data=BENCH.default_data()))

    def test_sanitize_keeps_structure_and_drops_unknown_keys(self):
        dirty = {"degree_base": {"bachelor": 9000},   # 值形状不对 + key 不在白名单
                 "direction_coef": {"ai": 1.2},
                 "city_tiers": {"t1": [1.3, "一线"]}}
        cleaned = BENCH.sanitize_data(dirty)
        self.assertIsNotNone(cleaned)
        # 三张表都必须完整（缺 key 用默认兜底），否则下游查表会 KeyError
        self.assertEqual(set(cleaned["degree_base"]), set(BENCH.DEGREE_BASE))
        self.assertEqual(set(cleaned["direction_coef"]), set(BENCH.DIRECTION_COEF))
        self.assertEqual(set(cleaned["city_tiers"]), set(BENCH.CITY_TIERS))
        # 不认识的 key 要能被识别出来（否则用户看到「导入成功」但数值没变）
        unknown = BENCH.unknown_keys(dirty)
        self.assertEqual(unknown.get("degree_base"), ["bachelor"])
        self.assertEqual(unknown.get("city_tiers"), ["t1"])

    def test_sanitize_rejects_non_dict(self):
        self.assertIsNone(BENCH.sanitize_data(None))
        self.assertIsNone(BENCH.sanitize_data([1, 2]))
        self.assertIsNone(BENCH.sanitize_data({"degree_base": {"phd": [1, 2]}}))


class TestSalaryReference(_StoreCase):

    def test_reference_carries_ask_and_data_freshness(self):
        """参考报价必须是**三档**（开口/目标/底线）+ 说清用的是哪一版行情。"""
        fake = _FakeChat()
        out = NG.get_salary_reference("张三\n某大学 计算机", "北京", "后端开发",
                                      chat_fn=fake)
        for key in ("suggest_ask", "suggest_target", "suggest_bottom",
                    "data_version", "data_year", "data_source", "data_updated_at"):
            self.assertIn(key, out, "缺少字段 %s" % key)
        self.assertGreater(out["suggest_ask"], out["suggest_target"])
        self.assertGreater(out["suggest_target"], out["suggest_bottom"])
        self.assertEqual(out["data_version"], 0)
        self.assertEqual(out["data_source"], "内置基线")

    def test_reference_uses_imported_benchmark(self):
        """导入权威数据后，同一次测算必须给出（明显更高的）新价 —— 证明数据真的生效。"""
        fake = _FakeChat()
        before = NG.get_salary_reference("张三", "北京", "后端开发", chat_fn=fake)
        table = BENCH.default_data()
        table["degree_base"]["bachelor_normal"] = [16000, 24000]
        BSTORE.refresh(table, data_year="2027", source="某机构 2027 届报告")
        after = NG.get_salary_reference("张三", "北京", "后端开发", chat_fn=fake)
        self.assertGreater(after["salary_range"]["low"], before["salary_range"]["low"])
        self.assertEqual(after["data_year"], "2027")
        self.assertEqual(after["data_version"], 1)

    def test_reference_survives_analysis_failure(self):
        """解读文案失败不能把数字一起丢掉 —— 数字是规则算的，不依赖模型。"""
        fake = _FakeChat(raise_on="你是资深薪酬顾问")
        out = NG.get_salary_reference("张三", "北京", "后端开发", chat_fn=fake)
        self.assertEqual(out["analysis"], "")
        self.assertGreater(out["salary_range"]["high"], 0)

    def test_honors_basic_degree_upgrade(self):
        """本科 + 985/211 要升到精英本科档（与服务端同规则）。"""
        fake = _FakeChat(profile={
            "degree": "本科", "school_tier": "985/211", "direction": "技术研发",
            "position_raw": "后端", "work_years": 0, "highlights": []})
        out = NG.get_salary_reference("张三", "北京", "", chat_fn=fake)
        self.assertIn("985/211", out["degree"])

    def test_reference_degrades_when_profile_model_fails(self):
        """★ 画像提取这一步模型挂了，不能把整块「算市场价」判死。

        此前 `get_salary_reference` 的画像调用**没有** try/except —— 本机模型不可用时
        直接抛异常，用户什么都拿不到；而同一函数的**解读**那一步是降级的
        （见 `test_reference_survives_analysis_failure`），两者失败语义自相矛盾。
        修后：改用简历原文跑规则识别学历/院校、用「心仪岗位」定方向，
        并在 caveats 里如实说明（`degraded=True`）。
        """
        fake = _FakeChat(raise_on=P.SALARY_PROFILE_SYSTEM)
        out = NG.get_salary_reference("李四\n985 本科 计算机科学与技术", "北京",
                                      "AI 算法工程师", chat_fn=fake)
        self.assertGreater(out["salary_range"]["high"], 0, "规则数字必须仍然给出")
        self.assertIn("985/211", out["degree"], "学历应能从简历原文规则识别")
        self.assertEqual(out["direction"], "AI/算法/大模型", "方向应取自「心仪岗位」")
        self.assertTrue(out["degraded"])
        self.assertTrue(any("模型不可用" in c for c in out["caveats"]),
                        "降级必须如实告知，不能假装正常")

    def test_profile_wins_when_model_available(self):
        """反向对照：模型可用时**不得**走兜底（学历/方向以画像为准）。"""
        fake = _FakeChat(profile={"degree": "硕士", "school_tier": "普通院校",
                                  "direction": "技术研发", "position_raw": "后端",
                                  "work_years": 3, "highlights": []})
        out = NG.get_salary_reference("李四\n985 本科", "北京", "AI 算法工程师", chat_fn=fake)
        self.assertEqual(out["degree"], "硕士", "画像可用时不得被简历原文覆盖")
        self.assertEqual(out["direction"], "技术研发")
        self.assertFalse(out["degraded"])
        self.assertFalse(any("模型不可用" in c for c in out["caveats"]))


class TestNegotiationSession(unittest.TestCase):

    def setUp(self):
        NG.reset_sessions()
        self.addCleanup(NG.reset_sessions)
        self.chat = _FakeChat()

    def test_finishes_exactly_at_max_rounds(self):
        """第 5 轮必须收口 —— 数字 5 是**服务端契约**，这里刻意写成字面量。

        用 `range(NG.MAX_ROUNDS)` 循环会让判据跟着常量走：常量被改成 6 时，
        用例会「自适应地」跟着跑 6 轮然后照样通过（反向对照抓到过这条假绿）。
        """
        self.assertEqual(NG.MAX_ROUNDS, 5, "与服务端 MAX_ROUNDS 不同值")
        session = NG.create_session("offer", "14000", "9000", "两段实习", chat_fn=self.chat)
        sid = session["id"]
        self.assertEqual(session["messages"][0]["role"], "hr")
        for i in range(4):                       # 前 4 轮都不该收口
            coaching, next_hr, finished, err = NG.submit_answer(sid, "回应%d" % i,
                                                                 chat_fn=self.chat)
            self.assertFalse(finished, "第 %d 轮不该结束" % (i + 1))
            self.assertIsNotNone(next_hr)
            self.assertIsNone(err)
        coaching, next_hr, finished, err = NG.submit_answer(sid, "第 5 轮",
                                                             chat_fn=self.chat)
        self.assertTrue(finished, "第 5 轮必须收口（服务端 MAX_ROUNDS=5）")
        self.assertIsNone(next_hr)
        self.assertEqual(NG.get_session(sid)["round_count"], 5)

    def test_model_call_runs_outside_sessions_lock(self):
        """与面试同构：教练点评 / HR 下一句两次模型调用都必须在锁外。

        判据同 `TestInterviewSession.test_model_call_runs_outside_sessions_lock`。
        """
        sid = NG.create_session("offer", "14000", "9000", "两段实习",
                                chat_fn=self.chat)["id"]
        probe = {"calls": 0, "free": True}

        def chat_fn(system, user):
            # 粘性探针，理由见 TestInterviewSession 同名用例（两次调用不能被后者覆盖）
            got = NG._SESSIONS_LOCK.acquire(timeout=1.0)
            if got:
                NG._SESSIONS_LOCK.release()
            probe["calls"] += 1
            probe["free"] = probe["free"] and got
            return self.chat(system, user)

        NG.submit_answer(sid, "我的回应", chat_fn=chat_fn)
        self.assertGreaterEqual(probe["calls"], 2, "两次模型回调都要跑到（判据非空靶）")
        self.assertTrue(probe["free"], "模型调用期间仍持有 _SESSIONS_LOCK")

    def test_round_count_advances_even_when_model_fails(self):
        """模型挂了也要累加轮次 —— 否则用户永远走不到收口，被卡在同一个 HR 问题上。"""
        sid = NG.create_session("offer", "14000", "9000", "", chat_fn=self.chat)["id"]
        with mock.patch.object(NG, "coach_answer", side_effect=RuntimeError("模型挂了")):
            coaching, next_hr, finished, err = NG.submit_answer(sid, "回应",
                                                                chat_fn=self.chat)
        self.assertIsNone(coaching)
        self.assertIsNotNone(err, "失败必须给出原因，不能静默")
        self.assertEqual(NG.get_session(sid)["round_count"], 1)
        self.assertFalse(finished)

    def test_unknown_session(self):
        coaching, next_hr, finished, err = NG.submit_answer("local-nego-nope", "x",
                                                            chat_fn=self.chat)
        self.assertIsNone(coaching)
        self.assertFalse(finished)
        self.assertIn("不存在", err or "")

    def test_answer_after_finish_is_idempotent(self):
        sid = NG.create_session("offer", "14000", "9000", "", chat_fn=self.chat)["id"]
        NG.finish_session(sid)
        coaching, next_hr, finished, err = NG.submit_answer(sid, "再答", chat_fn=self.chat)
        self.assertTrue(finished)
        self.assertIsNone(next_hr)
        self.assertEqual(NG.get_session(sid)["round_count"], 0, "结束后不该再累加轮次")

    def test_summary_shape(self):
        sid = NG.create_session("counter", "14000", "9000", "业绩点", chat_fn=self.chat)["id"]
        NG.submit_answer(sid, "回应", chat_fn=self.chat)
        out = NG.summary_of(NG.get_session(sid), chat_fn=self.chat)
        for key in ("summary", "round_count", "messages", "coaching", "status",
                    "scenario", "target_salary", "bottom_salary"):
            self.assertIn(key, out)
        self.assertEqual(out["scenario"], "counter")


class TestSalaryApiLocal(_StoreCase):

    def test_negotiation_roundtrip_stays_local(self):
        with mock.patch.object(AC, "_request") as req, \
             mock.patch.object(NG, "chat", _FakeChat()):
            started = AC.start_negotiation("offer", "14000", "9000", "两段实习")
            self.assertTrue(started["session_id"])
            self.assertTrue(started["hr_message"])
            stepped = AC.submit_negotiation_answer(started["session_id"], "我理解预算有限，但…")
            self.assertTrue(stepped["coaching"])
            self.assertIsNotNone(stepped["next_hr_message"])
            summary = AC.get_negotiation_summary(started["session_id"])
        req.assert_not_called()
        self.assertIn("summary", summary)

    def test_unknown_session_raises(self):
        with self.assertRaises(AC.ApiError):
            AC.submit_negotiation_answer("local-nego-nope", "x")
        with self.assertRaises(AC.ApiError):
            AC.get_negotiation_summary("local-nego-nope")

    def test_reference_requires_resume(self):
        with mock.patch.object(AC, "get_resume", return_value=None):
            with self.assertRaises(AC.ApiError) as ctx:
                AC.salary_reference("r-x", "北京", "后端")
        self.assertIn("简历", str(ctx.exception))

    def test_reference_is_local(self):
        with mock.patch.object(AC, "get_resume", return_value=dict(_RESUME)), \
             mock.patch.object(AC, "_request") as req, \
             mock.patch.object(NG, "chat", _FakeChat()):
            out = AC.salary_reference("r-0001", "北京", "后端开发")
        req.assert_not_called()
        self.assertIn(("suggest_ask"), out)


# ============ 4. 匹配层 ============

class TestMatchLocal(_StoreCase):

    def _calc(self, jd=None, resume=None, chat=None):
        with mock.patch.object(AC, "get_resume", return_value=dict(resume or _RESUME)), \
             mock.patch.object(AC, "get_jd", return_value=dict(jd or _JD)), \
             mock.patch.object(AC, "_request") as req, \
             mock.patch.object(MT, "chat", chat or _FakeChat()):
            out = AC.calculate_match("jd-0001", "r-0001")
        return out, req

    def test_returns_web_shaped_payload(self):
        out, req = self._calc()
        req.assert_not_called()
        for key in ("score", "skill_match_detail", "gap_analysis", "suggestion",
                    "jd_id", "resume_id"):
            self.assertIn(key, out)
        detail = out["skill_match_detail"]
        for key in ("matched", "missing", "partial"):
            self.assertIn(key, detail)
        self.assertIsInstance(out["score"], int)

    def test_result_is_persisted_locally(self):
        """算完必须落本机 —— 否则「匹配排行」里永远看不到刚算的这条。"""
        out, _ = self._calc()
        items = local_store.list_items("matches")
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["id"], "jd-0001:r-0001")
        self.assertEqual(items[0]["score"], out["score"])
        self.assertEqual(items[0]["_local_state"], "synced")

    def test_missing_resume_or_jd_raises(self):
        with mock.patch.object(AC, "get_resume", return_value=None), \
             mock.patch.object(AC, "get_jd", return_value=dict(_JD)):
            with self.assertRaises(AC.ApiError):
                AC.calculate_match("jd-0001", "r-0001")
        with mock.patch.object(AC, "get_resume", return_value=dict(_RESUME)), \
             mock.patch.object(AC, "get_jd", return_value=None):
            with self.assertRaises(AC.ApiError):
                AC.calculate_match("jd-0001", "r-0001")

    def test_ranking_prefers_local_over_cloud(self):
        """同一个 (jd, resume) 两边都有时**本机优先** —— 用户看的就是本机算的那个分。"""
        self._calc()
        cloud = [{"jd_id": "jd-0001", "resume_id": "r-0001", "score": 11,
                  "company": "某公司", "position": "后端", "suggestion": "云端口径"}]
        with mock.patch.object(AC, "_request", return_value=cloud):
            rows = AC.list_matches()
        self.assertEqual(len(rows), 1)
        self.assertNotEqual(rows[0]["score"], 11, "云端分覆盖了本机分")
        self.assertEqual(rows[0]["suggestion"][:2], "匹配")

    def test_ranking_keeps_both_sources_distinct(self):
        """两边是**不同**的 (jd, resume) 时都要保留（只读一边会漏掉另一边）。"""
        self._calc()
        cloud = [{"jd_id": "jd-9999", "resume_id": "r-0001", "score": 77,
                  "company": "另一家", "position": "算法", "suggestion": "云端"}]
        with mock.patch.object(AC, "_request", return_value=cloud):
            rows = AC.list_matches()
        self.assertEqual(len(rows), 2, "两边不同的记录必须都保留")
        self.assertIn(77, {r["score"] for r in rows})
        self.assertEqual(rows[0]["score"], max(r["score"] for r in rows), "没按降序排")

    def test_ranking_degrades_to_local_when_offline(self):
        self._calc()
        with mock.patch.object(AC, "_request", side_effect=AC.ApiOfflineError("断网")):
            rows = AC.list_matches()
        self.assertEqual(len(rows), 1, "断网时本机记录必须还在")

    def test_ranking_never_calls_network_when_logged_out(self):
        """未登录 → 云端那条路必然失败；本机记录仍要能读出来，且不能抛错。"""
        self._calc()
        with mock.patch.object(AC, "_request", side_effect=AC.ApiAuthError("未登录")):
            rows = AC.list_matches()
        self.assertEqual(len(rows), 1)

    def test_semantic_llm_path_marks_all_matched(self):
        """LLM 语义判定可用时按判定结果给分（不是一律走本地降级）。"""
        out, _ = self._calc(chat=_FakeChat())
        self.assertGreater(out["score"], 0)
        self.assertTrue(out.get("requirement_detail"), "缺少逐条判定，界面无法解释分数")


# ============ 5. 同源 / 单一事实源层 ============

class TestSingleSourceOfTruth(unittest.TestCase):

    def test_finish_negotiation_worker_is_started(self):
        """`_finish_negotiation` 必须真的 `start()`。

        原代码建了 worker、接了信号，却漏了 `start()`：谈判一结束遮罩就永远挂着，
        总结再也不出现 —— 而且不报任何错，是典型的「静默失效」。
        """
        tree = ast.parse(_read(_SALARY_PAGE))
        fn = _find_func(tree, "_finish_negotiation")
        self.assertIsNotNone(fn)
        attrs = _called_attrs(fn)
        self.assertIn("start", attrs, "_finish_negotiation 里的 worker 没有 start()")
        self.assertIn("connect", attrs, "信号没接上（判断为空靶）")

    def test_round_limit_has_a_single_source(self):
        """轮数上限只能有一个真源（`services.salary_negotiation.MAX_ROUNDS`）。

        两处各写一份时，改了执行侧、忘改展示侧 → 界面写「最多 5 轮」而实际跑 3 轮，
        且没有任何报错。
        """
        self.assertEqual(NG.MAX_ROUNDS, 5)
        cfg = ast.parse(_read(_CONFIG_PATH))
        names = {t.id for node in ast.walk(cfg) if isinstance(node, ast.Assign)
                 for t in node.targets if isinstance(t, ast.Name)}
        self.assertNotIn("MAX_NEGOTIATION_ROUNDS", names,
                         "config.py 又出现了第二份轮数上限")
        page = _read(_SALARY_PAGE)
        self.assertIn("services.salary_negotiation import MAX_ROUNDS", page,
                      "界面没有引用执行侧的真源")
        self.assertNotIn("config import MAX_NEGOTIATION_ROUNDS", page)

    def test_interview_limit_is_a_named_constant_used_by_the_judge(self):
        """上限判据必须引用常量，不能写死数字（写死就会和展示 / 文档漂开）。"""
        self.assertEqual(IV.MAX_QUESTIONS, 8)
        src = _read(os.path.join(_PKG, "services", "interview.py"))
        fn = _find_func(ast.parse(src), "submit_answer")
        body = ast.get_source_segment(src, fn)
        self.assertIn("MAX_QUESTIONS", body,
                      "收口判据里是硬编码数字，不是常量")

    def test_prompts_carry_ask_placeholder(self):
        """薪资解读提示词必须在**两处**都带开口价 —— 只在一处不够。

        - 「市场参考」段：给出三个价位数字的出处；
        - 【怎么开口】段：把「报开口价、别报目标价」讲成可执行的话术。

        只断言 `"{ask}" in 提示词` 是空靶：两处任一处被删掉，另一处还会让整串命中
        （反向对照当场抓到过这条假绿）。
        """
        self.assertIn("建议开口价：{ask} 元", P.SALARY_ANALYSIS_SYSTEM,
                      "「市场参考」段丢了开口价")
        self.assertIn("开口价 {ask} 元", P.SALARY_ANALYSIS_SYSTEM,
                      "【怎么开口】段丢了开口价")
        self.assertIn("开口报价", P.NEGOTIATION_COACH_SYSTEM)
        self.assertIn("底线", P.NEGOTIATION_COACH_SYSTEM)
        # 三条谈判提示词都会走 .format() ⇒ 条款里不能有花括号（会 KeyError）
        for name in ("NEGOTIATION_HR_SYSTEM", "NEGOTIATION_COACH_SYSTEM",
                     "SALARY_ANALYSIS_SYSTEM"):
            text = getattr(P, name)
            body = text
            for slot in ("scenario", "hr_message", "user_message", "target", "bottom",
                         "context", "degree_label", "school_label", "direction_label",
                         "position", "city_label", "experience_label", "highlights",
                         "low", "high", "ask", "basis", "playbook"):
                body = body.replace("{%s}" % slot, "")
            self.assertNotIn("{", body, "%s 里有多余的占位符，format 会炸" % name)

    def test_guard_clauses_survive_in_new_prompts(self):
        """新增 / 改动的提示词必须仍挂着防注入条款。

        用真源常量比对，不抄字面量 —— 条款改文案不会假红，漏挂立刻被抓到。
        """
        for name in ("SALARY_PROFILE_SYSTEM", "SALARY_ANALYSIS_SYSTEM",
                     "NEGOTIATION_HR_SYSTEM", "NEGOTIATION_COACH_SYSTEM",
                     "NEGOTIATION_SUMMARY_SYSTEM", "MOCK_INTERVIEWER_SYSTEM",
                     "MOCK_EVALUATOR_SYSTEM", "MOCK_SUMMARY_SYSTEM",
                     "INTERVIEW_QUESTIONS_SYSTEM"):
            text = getattr(P, name)
            self.assertIn(P._INJECTION_GUARD.strip(), text, "%s 漏挂防注入条款" % name)


if __name__ == "__main__":
    unittest.main(verbosity=2)
