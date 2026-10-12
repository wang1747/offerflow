"""桌面版审计 6 项逻辑修复「回灌网页版」后的回归用例。

每项都按「正向（修复后的行为）+ 反向对照（旧行为必须不成立）」两条判据写，
断言全部落在**具体字面量**上，不跟着被测常量走 —— 防止出现「恒绿空靶」。

对应关系（桌面版 → 网页版）：
  #6  出题归一        services/interview.py:normalize_question
                      → agents/graphs/interview_prep.py:normalize_question
  #7  全场题单 + 去重  services/interview.py:generate_question(asked_questions=)
                      → agents/graphs/mock_interview.py:generate_question(asked_questions=)
  #9  数字保真连单位   services/interview.py:check_answer_fidelity
                      → agents/graphs/interview_prep.py:check_answer_fidelity
  #9b 薪资三桶报告     core/salary_benchmark.py:sanitize_data(report=)
                      → core/salary_benchmark.py:sanitize_data(report=)
  #10 提示词防重复     services/prompts.py:MOCK_INTERVIEWER_SYSTEM
                      → agents/graphs/mock_interview.py:INTERVIEWER_PROMPT
  #11 面经查重分段指纹 services/interview.py:_dedup_normalize/_shingles
                      → core/text_similarity.py + modules/interview/services.py:_find_duplicate
"""
import contextlib
import difflib
import inspect
import os

# ============================== #6 出题归一 ==============================


def test_6_normalize_question_accepts_aliases():
    """模型写「技术题 / medium」这类别名，必须归一成规范值而不是整条丢弃。"""
    from app.agents.graphs.interview_prep import normalize_question

    q = normalize_question({"question": "  讲讲你的 RAG 分块策略  ",
                            "category": "技术题", "difficulty": "medium",
                            "answer": "..."})
    assert q is not None
    assert q["category"] == "技术"
    assert q["difficulty"] == "中等"
    assert q["question"] == "讲讲你的 RAG 分块策略"


def test_6_normalize_question_rejects_garbage():
    """真正不合法的输入仍要挡掉（归一不是放行一切）。"""
    from app.agents.graphs.interview_prep import normalize_question

    assert normalize_question({"question": "题", "category": "烹饪", "difficulty": "中等"}) is None
    assert normalize_question({"question": "", "category": "技术", "difficulty": "中等"}) is None
    assert normalize_question({"question": "题", "category": "技术", "difficulty": "地狱"}) is None
    assert normalize_question("not a dict") is None
    assert normalize_question(None) is None


def test_6_backward_compat_validate_question_name_kept():
    """旧判据名保留（历史调用方不必改），且它现在认别名。"""
    from app.agents.graphs.interview_prep import _validate_question

    assert _validate_question({"question": "题", "category": "technical",
                               "difficulty": "easy"}) is True
    assert _validate_question({"question": "题", "category": "烹饪",
                               "difficulty": "easy"}) is False


def test_6_node_reports_dropped_count():
    """graph 必须把「模型给了几条 / 丢了几条」交出来（否则界面只能笼统说重试）。"""
    from app.agents.graphs.interview_prep import InterviewState

    assert "raw_count" in InterviewState.__annotations__
    assert "dropped_count" in InterviewState.__annotations__


# ============================== #9 数字保真（含单位） ==============================


def test_9_flags_number_absent_from_resume():
    from app.agents.graphs.interview_prep import check_answer_fidelity

    assert check_answer_fidelity("参与后端开发", "把响应时间降低 40%") == ["40%"]


def test_9_flags_time_unit_mismatch():
    """核心新增判据：数字相同、时间单位不同 ⇒ 必须标记。

    旧版 `_number_core("3 个月") == "3"` 与简历里的 3 相等，等于把「3 年」
    当成「3 个月」的出处，一处编造被静默放行。
    """
    from app.agents.graphs.interview_prep import check_answer_fidelity

    got = check_answer_fidelity("有 3 年开发经验", "做了 3 个月")
    assert "3 个月" in got


def test_9_same_unit_is_not_flagged():
    """反向对照：单位一致时不许误报。"""
    from app.agents.graphs.interview_prep import check_answer_fidelity

    assert check_answer_fidelity("有 3 年开发经验", "做了 3 年") == []


def test_9_chinese_numerals_count_as_source():
    """反向对照：简历写「两段」、答案写「2 段」属于有出处，不能误报。"""
    from app.agents.graphs.interview_prep import check_answer_fidelity

    assert check_answer_fidelity("两段实习经历", "2 段实习") == []


def test_9_no_unit_number_still_compared():
    from app.agents.graphs.interview_prep import check_answer_fidelity

    # 返回的是原文 token（含单位），这样界面标红时能直接把原话指出来
    assert check_answer_fidelity("服务 12 个业务方", "服务 30 个业务方") == ["30 个"]
    assert check_answer_fidelity("服务 12 个业务方", "服务 12 个业务方") == []


# ============================== #9b 薪资三桶报告 ==============================


def test_9b_sanitize_reports_three_buckets():
    from app.core import salary_benchmark as bench

    dirty = {
        "degree_base": {"associate": [4000, 6000],
                        "bachelor_normal": [0, 100],       # low=0 非法 ⇒ 被替换
                        "bogus_key": [1, 2]},              # 不在白名单 ⇒ 被丢弃
        "direction_coef": {"ai": [1.45, "AI/算法"],
                           "unknown_dir": [1.0, "X"]},     # 不在白名单 ⇒ 被丢弃
        "city_tiers": {},                                  # 整表没给 ⇒ 全量补齐
    }
    report: dict = {}
    cleaned = bench.sanitize_data(dirty, report=report)

    assert cleaned is not None
    assert report["unknown_keys"]["degree_base"] == ["bogus_key"]
    assert report["unknown_keys"]["direction_coef"] == ["unknown_dir"]
    assert "bachelor_normal" in report["replaced_keys"]["degree_base"]
    assert report["missing_keys"]["city_tiers"]           # 一行都没给 ⇒ 记缺失
    # 被替换的 key 必须真的落回内置默认值（报告与结果一致，不是两张皮）
    assert cleaned["degree_base"]["bachelor_normal"] == list(bench.DEGREE_BASE["bachelor_normal"])


def test_9b_report_is_optional_and_result_identical():
    """反向对照：不传 report 时清洗结果必须完全一致（report 是纯出参）。"""
    from app.core import salary_benchmark as bench

    dirty = {"degree_base": {"associate": [4000, 6000]},
             "direction_coef": {"ai": [1.45, "AI/算法"]},
             "city_tiers": {}}
    report: dict = {}
    with_report = bench.sanitize_data(dirty, report=report)
    without_report = bench.sanitize_data(dirty)
    assert with_report == without_report
    assert report                                        # 但 report 确实被填了


def test_9b_unknown_keys_helper():
    from app.core import salary_benchmark as bench

    assert bench.unknown_keys({"degree_base": {"associate": [1, 2], "bachelor": [1, 2]}}) \
        == {"degree_base": ["bachelor"]}
    assert bench.unknown_keys({}) == {}


def test_9b_import_benchmark_surfaces_report():
    """导入入口必须把 report 透传出来（否则界面看不到「哪些字段没生效」）。"""
    from app.modules.salary_negotiation import services as nego_services

    src = inspect.getsource(nego_services.import_benchmark)
    assert "data_report" in src
    assert "report" in src


# ============================== #7 全场题单 + 去重 / #10 提示词 ==============================


def test_10_interviewer_prompt_forbids_repeat():
    from app.agents.graphs.mock_interview import INTERVIEWER_PROMPT

    assert "不要重复已经问过的问题" in INTERVIEWER_PROMPT


def test_7_generate_question_accepts_asked_questions():
    """反向对照：旧版签名没有这个参数（这里断言的就是旧版缺失的能力）。"""
    from app.agents.graphs import mock_interview as mi

    params = inspect.signature(mi.generate_question).parameters
    assert "asked_questions" in params
    assert params["asked_questions"].default is None


class _SeqLLM:
    """按顺序吐答案的假 LLM，同时记录每次收到的 user prompt。"""

    def __init__(self, answers):
        self._answers = list(answers)
        self.prompts = []

    def invoke(self, messages):
        self.prompts.append(messages[-1].content)

        class _R:
            content = ""
        r = _R()
        r.content = self._answers.pop(0) if self._answers else ""
        return r


def _patch_llm(monkeypatch, llm):
    from app.agents.graphs import mock_interview as mi

    monkeypatch.setattr(mi, "get_user_llm_or_raise", lambda _user: llm)
    monkeypatch.setattr(mi, "trace_operation", lambda *a, **k: contextlib.nullcontext())


def test_7_whole_question_list_is_fed_to_model(monkeypatch):
    from app.agents.graphs import mock_interview as mi

    llm = _SeqLLM(["全新考点：讲讲你对 FastAPI 依赖注入的理解"])
    _patch_llm(monkeypatch, llm)

    out = mi.generate_question("简历", "JD", user=object(),
                               asked_questions=["请讲讲你的 RAG 项目"])
    assert out == "全新考点：讲讲你对 FastAPI 依赖注入的理解"
    assert "已经问过的题目" in llm.prompts[0]
    assert "请讲讲你的 RAG 项目" in llm.prompts[0]      # 全场题单真的喂进去了
    assert len(llm.prompts) == 1                        # 不重复 ⇒ 不重试


def test_7_duplicate_question_triggers_one_retry(monkeypatch):
    from app.agents.graphs import mock_interview as mi

    llm = _SeqLLM(["请讲讲你的 RAG 项目",             # 第 1 次：与已问重复
                   "换个考点：讲讲索引失效的场景"])       # 第 2 次：新题
    _patch_llm(monkeypatch, llm)

    out = mi.generate_question("简历", "JD", user=object(),
                               asked_questions=["请讲讲你的 RAG 项目"])
    assert len(llm.prompts) == 2                        # 命中重复 ⇒ 重试了一次
    assert "重复" in llm.prompts[1]                     # 重试提示词里点明了原因
    assert out == "换个考点：讲讲索引失效的场景"


def test_7_retry_duplicate_returns_anyway_no_deadloop(monkeypatch):
    """反向对照：连续两次重复时**不能死循环**，必须照原样返回并退出。"""
    from app.agents.graphs import mock_interview as mi

    llm = _SeqLLM(["请讲讲你的 RAG 项目", "请讲讲你的 RAG 项目"])
    _patch_llm(monkeypatch, llm)

    out = mi.generate_question("简历", "JD", user=object(),
                               asked_questions=["请讲讲你的 RAG 项目"])
    assert len(llm.prompts) == 2                        # 最多重试一次
    assert out                                          # 仍有内容可返回


def test_7_no_asked_questions_no_extra_call(monkeypatch):
    """第一题（题单为空）不该多打一次模型。"""
    from app.agents.graphs import mock_interview as mi

    llm = _SeqLLM(["第一题：自我介绍"])
    _patch_llm(monkeypatch, llm)

    assert mi.generate_question("简历", "JD", user=object()) == "第一题：自我介绍"
    assert len(llm.prompts) == 1
    assert "已经问过的题目" not in llm.prompts[0]


# ============================== #11 面经查重分段指纹 ==============================


def test_11_dedup_normalize_ignores_punctuation_and_width():
    from app.core.text_similarity import dedup_normalize

    assert dedup_normalize("面试，问了：RAG（）") == dedup_normalize("面试,问了:RAG()")
    assert dedup_normalize("ＡＢＣ 测试") == dedup_normalize("ABC测试")


def test_11_is_dup_pair_catches_appended_content():
    """核心新增判据：补了几段的长面经，整段相似度掉到阈值下但指纹仍命中。"""
    from app.core.text_similarity import dedup_normalize, fingerprint_containment, is_dup_pair

    base = "面试官先问了项目经历，然后深挖 RAG 的分块策略与检索失败排查思路。" * 20
    extra = ("额外补充：向量检索召回不准时先看分块粒度，再看重排序模型，"
             "最后核对 embedding 是否与库一致。") * 30
    a, b = dedup_normalize(base), dedup_normalize(base + extra)
    assert len(a) > 60 and len(b) > len(a)

    ratio = difflib.SequenceMatcher(None, a, b).ratio()
    assert ratio < 0.9, "构造失败：整段相似度若仍 ≥0.9，这条用例就测不出旧版的漏判"
    assert fingerprint_containment(a, b) >= 0.9
    assert is_dup_pair(a, b) is True


def test_11_is_dup_pair_not_triggered_by_unrelated_text():
    """反向对照：两份不相干的面经不能被判重。"""
    from app.core.text_similarity import dedup_normalize, is_dup_pair

    a = dedup_normalize("问了 Python 基础、SQL 索引、并发写库的一致性。" * 10)
    b = dedup_normalize("全程聊大模型微调经验与显存优化，没有问算法题。" * 10)
    assert is_dup_pair(a, b) is False


def test_11_find_duplicate_uses_shared_predicate():
    """调用点必须真的走共用判据（旧版是自带的 SequenceMatcher + 只去空白）。"""
    from app.modules.interview import services as interview_services

    src = inspect.getsource(interview_services._find_duplicate)
    assert "is_dup_pair" in src
    assert "dedup_normalize" in src
    assert "SequenceMatcher" not in src


def test_11_submit_answer_passes_asked_questions():
    """#7 的调用点：提交回答时要把整场已问过的题目一起传下去。"""
    from app.modules.interview import services as interview_services

    src = inspect.getsource(interview_services.submit_interview_answer)
    assert "asked_questions" in src


# ============================== 新增模块可被正常导入 ==============================


def test_new_module_exists_and_exports():
    from app.core import text_similarity as ts

    for name in ("dedup_normalize", "shingles", "fingerprint_containment", "is_dup_pair"):
        assert callable(getattr(ts, name))
    assert os.path.basename(ts.__file__) == "text_similarity.py"
