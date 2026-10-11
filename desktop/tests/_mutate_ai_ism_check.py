#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""阶段 2（续）面试 / 匹配 / 薪资三个模块本机化的**反向对照**。

为什么必须有这一步：全绿的用例只说明「当前代码能过它」，不说明「换个错实现
它会拦下来」。下面这些情形用例照样会全绿 —— 假绿比不写用例更危险：

- **云端路径没切干净**：新写了 `_match.calculate_match`，但 `api_client.calculate_match`
  还在 `_request("POST", "/api/v1/match/")`。测试只要有结果就通过，用户却还在联网；
- **委派被换成空实现**（`return []`）：不再联网了，「不发网络」那条判据会很满意；
- **会话上限写错**（`>` 而不是 `>=`）、**失败时把 `feedback` 设成 None**
  （会被上层当成「会话不存在」）、**出题失败不收口**（卡在中间态出不去）；
- **开口价等于区间上限**（谈判一开局就失守）、**轮次不累加**（永远走不到收口）；
- **匹配结果不落本机**（排行永远看不到）、**排行不合并本机记录**（断网就空）；
- **导入的基准被静默换成默认值**、**版本号不再单调**（界面上的「第 N 版」会跳回去）。

做法：对生产源码做**精确字符串替换**（并校验锚点唯一）→ 跑指定用例 →
**无论结果都还原**。

⚠ 读写都必须 ``newline=""``：仓库约定 LF、``.gitattributes`` 是 ``* -text``、
打包产物按字节 sha256 校验。用默认文本模式回写会翻成 CRLF，产物哈希跟着变。

运行（必须用项目 venv、且 ``env -u PYTHONPATH`` 以避开沙箱 safe-delete shim）：
  env -u PYTHONPATH .venv/Scripts/python.exe tests/_mutate_ai_ism_check.py
"""

import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
PKG = os.path.join(ROOT, "offerflow_desktop")

AC = os.path.join(PKG, "api_client.py")                      # 接线切口
IV = os.path.join(PKG, "services", "interview.py")           # 面试会话状态机
NG = os.path.join(PKG, "services", "salary_negotiation.py")  # 薪资会话 + 报价
MB = os.path.join(PKG, "core", "salary_benchmark.py")        # 算价规则
BS = os.path.join(PKG, "core", "salary_benchmark_store.py")  # 基准数据本机版本化
PR = os.path.join(PKG, "services", "prompts.py")             # 薪资 / 面试提示词
SP = os.path.join(PKG, "ui", "pages", "salary.py")           # 薪资页（worker 必须 start）
RG = os.path.join(PKG, "ui", "pages", "resume_generate.py")  # 生成页（云端兜底必须挂在 llm_failed）
RO = os.path.join(PKG, "ui", "pages", "resume_optimize.py")  # 优化页（同上）

T_ISM = "test_ai_local_interview_salary_match.py"
T_SAL = "test_salary_benchmark.py"

MUTATIONS = [
    # ---------------- 接线层：云端路径必须真的切干净 ----------------
    {
        "id": "M1",
        "desc": "calculate_match 又走云端（新写了本地实现，旧路径照走）",
        "file": AC,
        "before": "    result = _match.calculate_match(jd, resume)",
        "after": '    result = _request("POST", "/api/v1/match/")',
        "test": T_ISM,
        "case": "TestNoCloudAnywhere.test_cloud_free_functions_never_call_request",
    },
    {
        "id": "M2",
        "desc": "基准版本信息又走云端",
        "file": AC,
        "before": "    return _bench_store.info()",
        "after": '    return _request("GET", "/api/v1/salary-negotiation/benchmark/info")',
        "test": T_ISM,
        "case": "TestNoCloudAnywhere.test_cloud_free_functions_never_call_request",
    },
    {
        "id": "M3",
        "desc": "生成面试题不再是委派（不联网了，但本机实现也没被调用）",
        "file": AC,
        "before": "    report = _interview.build_interview_questions(\n"
                  '        str(resume.get("raw_text") or ""), str(jd.get("raw_text") or ""),\n'
                  "        article_content=article_content, limit=limit or 10,\n"
                  "    )",
        "after": '    report = {"questions": [], "raw_count": 0, "dropped": 0}',
        "test": T_ISM,
        "case": "TestNoCloudAnywhere.test_cloud_free_functions_do_delegate_to_services",
    },
    # ---------------- 面试会话状态机 ----------------
    {
        "id": "M4",
        "desc": "题数上限判据写成 >（到第 9 题才收口）",
        "file": IV,
        "before": "        if question_count >= MAX_QUESTIONS:",
        "after": "        if question_count > MAX_QUESTIONS:",
        "test": T_ISM,
        "case": "TestInterviewSession.test_finishes_exactly_at_max_questions",
    },
    {
        "id": "M5",
        "desc": "点评失败时 feedback 设成 None（会被上层当成「会话不存在」）",
        "file": IV,
        "before": '        feedback = "（本轮点评生成失败，可继续回答或提前结束）"',
        "after": "        feedback = None",
        "test": T_ISM,
        "case": "TestInterviewSession.test_coach_failure_does_not_kill_the_session",
    },
    {
        "id": "M6",
        "desc": "已结束的会话又给反馈（静默续答）",
        "file": IV,
        "before": '            return {"feedback": None, "next_question": None,\n'
                  '                    "is_finished": True,\n'
                  '                    "question_number": len(session.get("questions") or [])}',
        "after": '            return {"feedback": "已结束", "next_question": None,\n'
                 '                    "is_finished": True,\n'
                 '                    "question_number": len(session.get("questions") or [])}',
        "test": T_ISM,
        "case": "TestInterviewSession.test_answer_after_finish_is_rejected_not_silently_continued",
    },
    {
        "id": "M7",
        "desc": "出下一题失败却不收口（会话卡在中间态，用户出不去）",
        "file": IV,
        "before": '        logger.warning("生成下一题失败：%s: %s", type(exc).__name__, exc)\n'
                  "        with _SESSIONS_LOCK:\n"
                  "            s = _SESSIONS.get(sid)\n"
                  "            if s is not None:\n"
                  '                s["status"] = "finished"\n'
                  '        return {"feedback": feedback, "next_question": None,\n'
                  '                "is_finished": True, "question_number": question_count}',
        "after": '        logger.warning("生成下一题失败：%s: %s", type(exc).__name__, exc)\n'
                 "        with _SESSIONS_LOCK:\n"
                 "            s = _SESSIONS.get(sid)\n"
                 "            if s is not None:\n"
                 '                s["status"] = "active"\n'
                 '        return {"feedback": feedback, "next_question": None,\n'
                 '                "is_finished": False, "question_number": question_count}',

        "test": T_ISM,
        "case": "TestInterviewSession.test_question_failure_closes_cleanly",
    },
    {
        "id": "M8",
        "desc": "题目分类不做归一（模型输出变体时整条被判无效）",
        "file": IV,
        "before": '    category = _norm_enum(item.get("category"), QUESTION_CATEGORY_ALIASES)',
        "after": '    category = "技术"',
        "test": T_ISM,
        "case": "TestInterviewSession.test_question_format_normalizes_aliases_and_keeps_real_questions",
    },
    {
        "id": "M9",
        "desc": "空简历也放行（第一题必然出不来）",
        "file": IV,
        "before": '        raise ValueError("简历内容为空，无法开始模拟面试")',
        "after": "        pass",
        "test": T_ISM,
        "case": "TestInterviewSession.test_empty_resume_is_rejected",
    },
    # ---------------- 面经离线导入 ----------------
    {
        "id": "M10",
        "desc": "离线导入不本机抽题（断网只能看到一条光秃秃的原文）",
        "file": AC,
        "before": "        merged = _extract_article_metadata_locally(item)",
        "after": "        merged = dict(item)",
        "test": T_ISM,
        "case": "TestArticleOfflineExtraction.test_offline_import_extracts_questions_locally",
    },
    {
        "id": "M11",
        "desc": "离线不做去重（反复导入会在本地堆出一串副本）",
        "file": AC,
        "before": "    dup = _interview.find_duplicate_article(text, existing)",
        "after": "    dup = None",
        "test": T_ISM,
        "case": "TestArticleOfflineExtraction.test_offline_duplicate_is_detected_locally",
    },
    # ---------------- 薪资：算价与基准数据 ----------------
    {
        "id": "M12",
        "desc": "开口价 = 区间上限（HR 一压就跌破真实期望）",
        # ⚠️ 必须**连兜底一起打**：`if ask <= high: ask = high + 1000` 会把
        # `ask = high` 修回「严格大于」—— 只砍主路径是空靶（反向对照抓到过）。
        "file": MB,
        "before": "    ask = _round_to_500(high * _ASK_PREMIUM)\n"
                  "    if ask <= high:                      "
                  "# 兜底：开口价严格高于区间上限\n"
                  "        ask = high + 1000\n",
        "after": "    ask = high\n",
        "test": T_ISM,
        "case": "TestSalaryBenchmarkRules.test_ask_is_highest_and_above_range_top",
    },
    {
        "id": "M13",
        "desc": "算价无视生效数据（导入了权威数据但不生效）",
        "file": MB,
        "before": '    degree_base = (data or {}).get("degree_base") or DEGREE_BASE',
        "after": "    degree_base = DEGREE_BASE",
        "test": T_ISM,
        "case": "TestSalaryBenchmarkRules.test_active_data_overrides_builtin_tables",
    },
    {
        "id": "M14",
        "desc": "unknown_keys 恒返回空（导入了白名单外的字段却报「成功」）",
        "file": MB,
        "before": "        unknown = sorted(str(k) for k in raw if k not in default_map)\n"
                  "        if unknown:\n"
                  "            out[name] = unknown",
        "after": "        pass",
        "test": T_ISM,
        "case": "TestSalaryBenchmarkRules.test_sanitize_keeps_structure_and_drops_unknown_keys",
    },
    {
        "id": "M15",
        "desc": "脏数据不拦、静默换成默认值（用户以为导入生效了）",
        "file": BS,
        "before": "    if not cleaned:\n"
                  '        raise ValueError("基准数据不合法（需要 degree_base / '
                  'direction_coef / city_tiers 三张表），已拒绝写入")',
        "after": "    if not cleaned:\n        cleaned = benchmark.default_data()",
        "test": T_SAL,
        "case": "TestApiClientLocalSemantics.test_import_rejects_incomplete_data_without_writing",
    },
    {
        "id": "M16",
        "desc": "版本号写死 1（界面上的「第 N 版」不再前进）",
        "file": BS,
        "before": '        version = int(cur.get("version") or 0) + 1',
        "after": "        version = 1",
        "test": T_SAL,
        "case": "TestApiClientLocalSemantics.test_import_writes_local_version_and_is_readable_back",
    },
    # ---------------- 薪资：谈判会话 ----------------
    {
        "id": "M17",
        "desc": "谈判轮次不累加（模型一挂就永远走不到收口）",
        "file": NG,
        "before": '        round_count = int(session.get("round_count") or 0) + 1\n'
                  '        session["round_count"] = round_count\n'
                  "        if round_count >= MAX_ROUNDS:",
        "after": '        round_count = int(session.get("round_count") or 0)\n'
                 '        session["round_count"] = round_count\n'
                 "        if round_count >= MAX_ROUNDS:",
        "test": T_ISM,
        "case": "TestNegotiationSession.test_round_count_advances_even_when_model_fails",
    },
    {
        "id": "M18",
        "desc": "谈判上限改成 6（界面写 5 轮、实际跑 6 轮）",
        "file": NG,
        "before": "MAX_ROUNDS = 5",
        "after": "MAX_ROUNDS = 6",
        "test": T_ISM,
        "case": "TestNegotiationSession.test_finishes_exactly_at_max_rounds",
    },
    {
        "id": "M19",
        "desc": "会话不存在时静默返回空（界面显示一条空消息）",
        "file": AC,
        "before": '    if coaching is None and next_hr is None and not finished:\n'
                  '        raise ApiError(error or "会话不存在")',
        "after": "    if False:\n"
                 '        raise ApiError(error or "会话不存在")',
        "test": T_ISM,
        "case": "TestSalaryApiLocal.test_unknown_session_raises",
    },
    # ---------------- 匹配 ----------------
    {
        "id": "M20",
        "desc": "匹配结果不落本机（排行里永远看不到刚算的这条）",
        "file": AC,
        "before": '    if not local_store.restore(_MATCH_ENTITY, mid, item, state="synced"):\n'
                  '        logger.debug("[matches] 本次匹配结果未落户本地（未登录或本地库不可用）：%s", mid)',
        "after": "    return",
        "test": T_ISM,
        "case": "TestMatchLocal.test_result_is_persisted_locally",
    },
    {
        "id": "M21",
        "desc": "排行不合并本机记录（断网 / 未登录时排行直接空掉）",
        "file": AC,
        "before": "    for item in _local_read(_MATCH_ENTITY):",
        "after": "    for item in []:",
        "test": T_ISM,
        "case": "TestMatchLocal.test_ranking_degrades_to_local_when_offline",
    },
    {
        "id": "M22",
        "desc": "排行不覆盖云端（同一个岗位的分数被云端口径换掉）",
        "file": AC,
        "before": "        merged[key] = _norm_match(dict(item))   # 本机覆盖云端",
        "after": "        merged.setdefault(key, _norm_match(dict(item)))",
        "test": T_ISM,
        "case": "TestMatchLocal.test_ranking_prefers_local_over_cloud",
    },
    {
        "id": "M23",
        "desc": "未登录时排行直接抛错（本机记录明明读得出来）",
        "file": AC,
        "before": "    except ApiAuthError:\n"
                  '        _state["served_from_cache"] = False    # 掉登录 ≠ 离线\n'
                  "    except (ApiOfflineError, ApiError) as exc:\n"
                  '        logger.info("[matches] 云端排行不可用，只用本机记录：%s", exc)',
        "after": "    except ApiAuthError:\n"
                 "        raise\n"
                 "    except (ApiOfflineError, ApiError) as exc:\n"
                 '        logger.info("[matches] 云端排行不可用，只用本机记录：%s", exc)',
        "test": T_ISM,
        "case": "TestMatchLocal.test_ranking_never_calls_network_when_logged_out",
    },
    # ---------------- 界面 / 提示词 ----------------
    {
        "id": "M24",
        "desc": "谈判结束的 worker 漏掉 start()（总结永远出不来、遮罩卡死）",
        "file": SP,
        "before": "        self._worker.failed.connect(self._on_failed)\n"
                  "        self._worker.start()\n\n"
                  "    def _on_neg_summary(self, result) -> None:",
        "after": "        self._worker.failed.connect(self._on_failed)\n\n"
                 "    def _on_neg_summary(self, result) -> None:",
        "test": T_ISM,
        "case": "TestSingleSourceOfTruth.test_finish_negotiation_worker_is_started",
    },
    {
        "id": "M25",
        "desc": "文案说成「对所有账号生效」（本机数据说成全局，误导用户）",
        "file": SP,
        "before": '            " —— 本机生效")',
        "after": '            " —— 该基准对所有账号生效")',
        "test": T_SAL,
        "case": "TestSalaryPageBenchmarkBar.test_import_success_uses_dialog_payload_and_says_local_only",
    },
    {
        "id": "M26",
        "desc": "薪资解读提示词「市场参考」段丢掉开口价",
        "file": PR,
        "before": "建议开口价：{ask} 元",
        "after": "建议报价：{target} 元",
        "test": T_ISM,
        "case": "TestSingleSourceOfTruth.test_prompts_carry_ask_placeholder",
    },
    {
        "id": "M27",
        "desc": "薪资解读提示词【怎么开口】段丢掉开口价",
        # ⚠️ 同一不变量由两处保证，必须**分别打**：只打一处时另一处还会命中，
        # 只断言 `"{ask}" in 提示词` 的用例照样全绿（反向对照抓到过）。
        "file": PR,
        "before": "第一次开口要报「开口价 {ask} 元」而不是「目标价 {target} 元」",
        "after": "第一次开口要往高了报，而不是直接报「目标价 {target} 元」",
        "test": T_ISM,
        "case": "TestSingleSourceOfTruth.test_prompts_carry_ask_placeholder",
    },
    # ---------------- 三类口径（本地优先写 / 本地回退读 / 云端只兜底） ----------------
    {
        "id": "M28",
        "desc": "upload_resume 倒成「先联网、失败才落本地」（顺序判据）",
        "file": AC,
        "before": '    seq = local_store.save("resumes", "create", local_id, item)\n'
                  "    if seq is None:\n"
                  '        data = _request("POST", "/api/v1/resume/upload", data=payload)',
        "after": "    seq = None\n"
                 "    if seq is None:\n"
                 '        data = _request("POST", "/api/v1/resume/upload", data=payload)',
        "test": T_ISM,
        "case": "TestNoCloudAnywhere.test_local_first_writers_touch_local_store_before_request",
    },
    {
        "id": "M29",
        "desc": "list_matches 不再合并本机记录（断网排行就空）",
        "file": AC,
        "before": "    for item in _local_read(_MATCH_ENTITY):",
        "after": "    for item in []:",
        "test": T_ISM,
        "case": "TestNoCloudAnywhere.test_local_fallback_readers_merge_local_data",
    },
    {
        "id": "M30",
        "desc": "生成页云端兜底不再判 llm_failed（变成无条件回落）",
        "file": RG,
        "before": '            if result.get("llm_failed"):\n'
                  "                return db.generate_resume(req, jd_text)",
        "after": '            if result.get("cloud_fallback"):\n'
                 "                return db.generate_resume(req, jd_text)",
        "test": T_ISM,
        "case": "TestNoCloudAnywhere.test_cloud_fallback_only_reached_under_llm_failed",
    },
    {
        "id": "M31",
        "desc": "优化页云端兜底不再判 llm_failed",
        "file": RO,
        "before": '            if result.get("llm_failed"):\n'
                  "                return db.optimize_resume(resume_id, jd_id)",
        "after": '            if result.get("cloud_fallback"):\n'
                 "                return db.optimize_resume(resume_id, jd_id)",
        "test": T_ISM,
        "case": "TestNoCloudAnywhere.test_cloud_fallback_only_reached_under_llm_failed",
    },
    {
        "id": "M32",
        "desc": "upload_resume 先联网再落本地 —— 离线导入直接炸（行为判据）",
        "file": AC,
        "before": '    seq = local_store.save("resumes", "create", local_id, item)\n'
                  "    if seq is None:\n"
                  '        data = _request("POST", "/api/v1/resume/upload", data=payload)',
        "after": "    seq = None\n"
                 "    if seq is None:\n"
                 '        data = _request("POST", "/api/v1/resume/upload", data=payload)',
        "test": T_ISM,
        "case": "TestLocalFirstDataPaths.test_upload_resume_works_offline",
    },
    {
        "id": "M33",
        "desc": "list_resume_versions 丢掉本地链 —— 断网后版本链一片空白",
        "file": AC,
        "before": "    for it in _local_chain(resume_id):",
        "after": "    for it in []:",
        "test": T_ISM,
        "case": "TestLocalFirstDataPaths.test_list_resume_versions_sees_local_chain_offline",
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
