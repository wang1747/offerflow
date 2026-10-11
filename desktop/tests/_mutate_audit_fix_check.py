#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""外部审查报告修复轮（#4 / #5 / #6 / #7 / #8 / #9 / #11）的**反向对照**。

为什么必须有这一步：全绿的用例只说明「当前代码能过它」，不说明「换个错实现
它会拦下来」。下面这些改法本文件的用例照样会全绿 —— 假绿比不写用例更危险：

- 页面销毁时**根本不断连**（回调摸已释放的 Qt 对象 → 0xC0000409 静默崩退）；
- 「刷新市场数据」死按钮**又加回来**（点了必然报错，用户以为坏了）；
- 出题格式校验退回严格白名单（模型输出变体时整条题被丢）；
- 出下一题**不带全场题单**、重复出题**不重试**（第 5 题重问第 2 题）；
- 数字保真**不连单位**（简历 3 年、答案 3 个月也算「有出处」）；
- 基准数据被静默兜底却**不报告**（用户以为导入生效了）；
- 面经查重**只去空白**（换个标点就能绕过）、**只比整段**（补几段就绕过）。

做法：对生产源码做**精确字符串替换**（并校验锚点唯一）→ 跑指定用例 →
**无论结果都还原**。

⚠ 读写都必须 ``newline=""``：仓库约定 LF、``.gitattributes`` 是 ``* -text``、
打包产物按字节 sha256 校验。用默认文本模式回写会翻成 CRLF，产物哈希跟着变。

运行（必须用项目 venv、且 ``env -u PYTHONPATH`` 以避开沙箱 safe-delete shim）：
  env -u PYTHONPATH .venv/Scripts/python.exe tests/_mutate_audit_fix_check.py
"""

import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
PKG = os.path.join(ROOT, "offerflow_desktop")

SAL = os.path.join(PKG, "ui", "pages", "salary.py")
IVP = os.path.join(PKG, "ui", "pages", "interview.py")
AC = os.path.join(PKG, "api_client.py")
IV = os.path.join(PKG, "services", "interview.py")
MB = os.path.join(PKG, "core", "salary_benchmark.py")

T_SAL = "test_salary_benchmark.py"
T_IVI = "test_interview_import.py"
T_ISM = "test_ai_local_interview_salary_match.py"

#: 三个文件里逐字相同的「断连循环」——分别打，确认两页各自都真的接了清理钩子。
_CLEANUP_LOOP = ("        for w in self._workers:\n"
                 "            try:\n"
                 "                w.success.disconnect()\n"
                 "                w.failed.disconnect()\n"
                 "            except Exception:  # noqa: BLE001 —— 测试替身可能没有 disconnect\n"
                 "                pass\n"
                 "        self._workers = []")

MUTATIONS = [
    # ---------------- #4 页面销毁断连 ----------------
    {
        "id": "A1",
        "desc": "薪资页不接 destroyed 钩子（页面销毁后回调照跑 → 摸已释放对象）",
        "file": SAL,
        "before": "        self.destroyed.connect(self._cleanup_workers)",
        "after": "        pass",
        "test": T_SAL,
        "case": "TestWorkerLifecycle.test_destroyed_signal_triggers_cleanup",
    },
    {
        "id": "A2",
        "desc": "薪资页清理时不 disconnect（信号照旧连着的）",
        "file": SAL,
        "before": _CLEANUP_LOOP,
        "after": "        self._workers = []",
        "test": T_SAL,
        "case": "TestWorkerLifecycle.test_cleanup_disconnects_both_signals",
    },
    {
        "id": "A3",
        "desc": "面试页不接 destroyed 钩子",
        "file": IVP,
        "before": "        self.destroyed.connect(self._cleanup_workers)",
        "after": "        pass",
        "test": T_IVI,
        "case": "TestWorkerLifecycle.test_destroyed_signal_triggers_cleanup",
    },
    {
        "id": "A4",
        "desc": "面试页清理时不 disconnect",
        "file": IVP,
        "before": _CLEANUP_LOOP,
        "after": "        self._workers = []",
        "test": T_IVI,
        "case": "TestWorkerLifecycle.test_cleanup_disconnects_both_signals",
    },
    # ---------------- #5 死按钮 ----------------
    {
        "id": "B1",
        "desc": "「刷新市场数据」死按钮又加回来（点了必然报错）",
        "file": SAL,
        "before": "        self.bench_import_btn.clicked.connect(self._open_benchmark_import)\n"
                  "        info_row.addWidget(self.bench_import_btn)",
        "after": "        self.bench_import_btn.clicked.connect(self._open_benchmark_import)\n"
                 "        info_row.addWidget(self.bench_import_btn)\n"
                 '        info_row.addWidget(QPushButton("刷新市场数据"))',
        "test": T_SAL,
        "case": "TestSalaryPageBenchmarkBar.test_refresh_button_is_gone",
    },
    # ---------------- #6 出题格式分流 ----------------
    {
        "id": "C1",
        "desc": "全部题被格式丢弃时不再报错（变成一句含糊的「未生成题目」）",
        "file": AC,
        "before": '    if report.get("raw_count") and not report.get("questions"):\n'
                  "        raise ApiError(\n"
                  '            f"模型返回的 {report[\'raw_count\']} 道题分类/难度都不符合要求，已全部丢弃。"\n'
                  '            "可在「模型设置」里换一个更强的模型后重试。")',
        "after": "    pass",
        "test": T_ISM,
        "case": "TestInterviewApiLocal.test_all_dropped_questions_report_format_error",
    },
    {
        "id": "C2",
        "desc": "丢弃条数恒为 0（诊断信息说谎）",
        "file": IV,
        "before": "    dropped = len(raw_questions) - len(valid)",
        "after": "    dropped = 0",
        "test": T_ISM,
        "case": "TestInterviewSession.test_build_report_counts_dropped_questions",
    },
    # ---------------- #7 全场题单 + 出题去重 ----------------
    {
        "id": "D1",
        "desc": "出下一题不帶全场题单（只给上一问上一答）",
        "file": IV,
        "before": "            previous_answer=answer,\n"
                  "            asked_questions=asked_questions,",
        "after": "            previous_answer=answer,\n"
                 "            asked_questions=None,",
        "test": T_ISM,
        "case": "TestInterviewSession.test_question_prompt_carries_every_asked_question",
    },
    {
        "id": "D2",
        "desc": "重复出题不重试（把与已问重复的题原样塞给用户）",
        "file": IV,
        "before": "    retry = _chat(prompts.MOCK_INTERVIEWER_SYSTEM, retry_prompt).strip()\n"
                  "    if retry and not is_duplicate_question(retry, asked):\n"
                  "        return retry",
        "after": "    retry = question",
        "test": T_ISM,
        "case": "TestInterviewSession.test_duplicate_question_triggers_one_retry",
    },
    # ---------------- #8 数字保真连单位 ----------------
    {
        "id": "E1",
        "desc": "数字保真不连单位（3 年 与 3 个月 互相当作出处）",
        "file": IV,
        "before": "        known_time = resume_units.get(num, set()) & _TIME_UNITS\n"
                  "        if unit in _TIME_UNITS and known_time and unit not in known_time:",
        "after": "        known_time = resume_units.get(num, set()) & _TIME_UNITS\n"
                 "        if False:",
        "test": T_ISM,
        "case": "TestAnswerFidelityNumbers.test_same_number_with_conflicting_time_unit_is_flagged",
    },
    {
        "id": "E2",
        "desc": "不认中文数字（「两段实习」与「2 段实习」误报为简历里没有）",
        "file": IV,
        "before": "    pairs = set(_number_tokens(resume_text)) | set(_cn_number_tokens(resume_text))",
        "after": "    pairs = set(_number_tokens(resume_text))",
        "test": T_ISM,
        "case": "TestAnswerFidelityNumbers.test_chinese_numeral_in_resume_covers_arabic_in_answer",
    },
    # ---------------- #9 静默兜底可诊断 ----------------
    {
        "id": "F1",
        "desc": "取值非法的字段不记进 replaced_keys（用户以为导入生效了）",
        "file": MB,
        "before": "                else:\n"
                  '                    _note(report, "replaced_keys", "degree_base", k)\n'
                  "                    cleaned[k] = list(default_v)\n"
                  "            else:\n"
                  '                _note(report, "replaced_keys", "degree_base", k)\n'
                  "                cleaned[k] = list(default_v)",
        "after": "                else:\n"
                 "                    cleaned[k] = list(default_v)\n"
                 "            else:\n"
                 "                cleaned[k] = list(default_v)",
        "test": T_SAL,
        "case": "TestSanitizeReportSource.test_reports_replaced_unknown_and_missing",
    },
    {
        "id": "F2",
        "desc": "导入返回体不挂 data_report（界面拿不到被兜底的字段）",
        "file": AC,
        "before": '        info["data_report"] = report',
        "after": "        pass",
        "test": T_SAL,
        "case": "TestApiClientLocalSemantics.test_import_reports_silently_replaced_fields",
    },
    # ---------------- #11 面经查重 ----------------
    {
        "id": "G1",
        "desc": "查重只去空白不去标点（换个标点就能绕过）",
        "file": IV,
        "before": r'''    normalized = unicodedata.normalize("NFKC", str(text or "")).lower()
    return re.sub(r"[\W_]+", "", normalized)''',
        "after": r'''    normalized = unicodedata.normalize("NFKC", str(text or "")).lower()
    return re.sub(r"\s+", "", normalized)''',
        "test": T_ISM,
        "case": "TestArticleDedupNormalization.test_dedup_normalize_strips_punctuation_not_just_whitespace",
    },
    {
        "id": "G2",
        "desc": "查重只比整段（补了几段再导入就绕过）",
        "file": IV,
        "before": "    sa, sb = _shingles(a_norm), _shingles(b_norm)\n"
                  "    if not sa or not sb:\n"
                  "        return False\n"
                  "    return len(sa & sb) / min(len(sa), len(sb)) >= 0.9",
        "after": "    return False",
        "test": T_ISM,
        "case": "TestArticleDedupNormalization.test_long_article_with_added_paragraph_still_dup",
    },
]


def run_case(test: str, case: str) -> bool:
    proc = subprocess.run(
        [sys.executable, "-u", os.path.join(HERE, test), case],
        cwd=ROOT, capture_output=True, text=True)
    return proc.returncode == 0


def main() -> int:
    sources = {}
    for path in {m["file"] for m in MUTATIONS}:
        with open(path, encoding="utf-8", newline="") as fh:
            sources[path] = fh.read()

    def _write(path: str, text: str) -> None:
        with open(path, "w", encoding="utf-8", newline="") as fh:
            fh.write(text)

    bad = []
    try:
        for mut in MUTATIONS:
            num, desc = mut["id"], mut["desc"]
            original = sources[mut["file"]]
            n = original.count(mut["before"])
            if n == 0:
                print("[%s] 锚点没匹配上，变异无效：%s" % (num, desc))
                bad.append("%s 锚点失配" % num)
                continue
            if n > 1:
                print("[%s] 锚点在文件里出现 %d 次，变异落点不确定：%s" % (num, n, desc))
                bad.append("%s 锚点不唯一" % num)
                continue
            if not run_case(mut["test"], mut["case"]):
                print("[%s] 基线就是红的，无法判断变异是否被拦住：%s"
                      % (num, mut["case"]))
                bad.append("%s 基线红" % num)
                continue

            mutated = original.replace(mut["before"], mut["after"])
            if mutated == original:
                print("[%s] 替换后内容没变，变异无效：%s" % (num, desc))
                bad.append("%s 空替换" % num)
                continue
            _write(mut["file"], mutated)
            try:
                passed = run_case(mut["test"], mut["case"])
            finally:
                _write(mut["file"], original)

            if passed:
                print("[%s] XX 变异后用例仍然通过 —— 判据是空靶：%s" % (num, desc))
                bad.append(num)
            else:
                print("[%s] OK 变异被判据拦下：%s" % (num, desc))
    finally:
        for path, original in sources.items():
            _write(path, original)

    print()
    if bad:
        print("反向对照未通过：%s" % ", ".join(bad))
        return 1
    print("反向对照全部通过（%d 处变异都被判据拦下）" % len(MUTATIONS))
    return 0


if __name__ == "__main__":
    sys.exit(main())
