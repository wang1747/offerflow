#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""阶段 2 本地优化链路（`services/resume_optimizer` + `core/preservation` + `services/match` + `prompts`）
的**反向对照**。

为什么必须有这一步：全绿的用例只说明「当前代码能过它」，不说明「换个错实现
它会拦下来」。护栏接歪了（阈值改回 0.60、编造不再拦、回退了却照样落库、
落点写错表、JD 原文被截断）时用例照样全绿 —— 假绿比没写用例更危险。

做法：对生产源码做**精确字符串替换**（并校验锚点唯一）→ 跑指定用例 →
**无论结果都还原**。

⚠ 读写都必须 ``newline=""``：仓库约定 LF、``.gitattributes`` 是 ``* -text``、
打包产物按字节 sha256 校验。用默认文本模式回写会翻成 CRLF，产物哈希跟着变。

运行（必须用项目 venv、且 ``env -u PYTHONPATH`` 以避开沙箱 safe-delete shim）：
  env -u PYTHONPATH .venv/Scripts/python.exe tests/_mutate_ai_optimize_check.py
"""

import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
PKG = os.path.join(ROOT, "offerflow_desktop")

PV = os.path.join(PKG, "core", "preservation.py")        # 保真判据（丢失 + 新增）
RO = os.path.join(PKG, "services", "resume_optimizer.py")  # 优化编排 + 落库
MA = os.path.join(PKG, "services", "match.py")             # JD 需求 → 提示词
PR = os.path.join(PKG, "services", "prompts.py")           # 优化提示词
AC = os.path.join(PKG, "api_client.py")                    # 本地版本链（列表/回滚）

T = "test_ai_local_optimize.py"

MUTATIONS = [
    # ---------------- 保真阈值（同义改写不能被误判成丢内容） ----------------
    {
        "id": "M1",
        "desc": "阈值改回旧的 0.60 → 正常同义改写被判「丢内容」而回退原文",
        "file": PV,
        "before": "# 0.35 仍能拦住整段替换，但不再要求逐句近似。\n_MIN_TERM_RETENTION = 0.35",
        "after": "# 0.35 仍能拦住整段替换，但不再要求逐句近似。\n_MIN_TERM_RETENTION = 0.60",
        "test": T,
        "case": "TestPreservationThreshold.test_synonym_rewrite_passes_below_old_threshold",
    },
    # ---------------- 新增事实（编造）拦截 ----------------
    {
        "id": "M2",
        "desc": "passed 判据丢掉 added_facts → 编造的公司/数字照样通过",
        "file": PV,
        "before": "    passed = critical_ok and not added_facts and term_score >= _MIN_TERM_RETENTION",
        "after": "    passed = critical_ok and term_score >= _MIN_TERM_RETENTION",
        "test": T,
        "case": "TestAddedFacts.test_new_org_relation_detected",
    },
    {
        "id": "M3",
        "desc": "新增事实抽取器返回空 → 编造检测形同虚设（另一条机制：判据本身没了）",
        "file": PV,
        "before": "    return list(dict.fromkeys(item for item in added if item))",
        "after": "    return []",
        "test": T,
        "case": "TestAddedFacts.test_new_number_detected",
    },
    # ---------------- 粘连联系方式（取到错值） ----------------
    {
        "id": "M4",
        "desc": "不再拆粘连联系人 → 邮箱被抽成 a.comhttps 这种错值",
        "file": PV,
        "before": '    return _GLUED_CONTACT.sub(" ", text or "")',
        "after": "    return text or \"\"",
        "test": T,
        "case": "TestGluedContacts.test_email_value_is_not_polluted",
    },
    # ---------------- 回退 / 兜底信号 ----------------
    {
        "id": "M5",
        "desc": "保真校验失败时不回退 → 编造稿直接当成优化结果给出",
        "file": RO,
        "before": '    if not preservation["passed"]:',
        "after": "    if False:",
        "test": T,
        "case": "TestOptimizeFlow.test_fabricated_result_is_rejected_and_not_saved",
    },
    {
        "id": "M6",
        "desc": "本地模型失败不再给 llm_failed 信号 → UI 不会回落云端，直接报错",
        "file": RO,
        "before": '        "llm_failed": llm_failed,',
        "after": '        "llm_failed": False,',
        "test": T,
        "case": "TestOptimizeFlow.test_llm_failure_signals_cloud_fallback",
    },
    {
        "id": "M7",
        "desc": "回退稿照样落库 → 历史里多出一份和原文一样的记录",
        "file": RO,
        "before": '    if result.get("error") or fallback:',
        "after": "    if False:",
        "test": T,
        "case": "TestOptimizeFlow.test_fabricated_result_is_rejected_and_not_saved",
    },
    {
        "id": "M16",
        "desc": "回退时不置 fallback 标记 → 界面分不清「原样返回」与「真的优化了」",
        "file": RO,
        "before": '            "score": 1.0, "passed": True, "fallback": True,',
        "after": '            "score": 1.0, "passed": True, "fallback": False,',
        "test": T,
        "case": "TestOptimizeFlow.test_fabricated_result_is_rejected_and_not_saved",
    },
    {
        "id": "M17",
        "desc": "空简历不再报错 → 用户以为优化过了",
        "file": RO,
        "before": '            "error": "原简历内容为空，请先上传可编辑文本的简历文件",',
        "after": '            "error": "",',
        "test": T,
        "case": "TestOptimizeFlow.test_empty_resume_reports_error",
    },
    {
        "id": "M14",
        "desc": "JD 为空也照样调模型 → 白烧一次 token 且结论无意义",
        "file": RO,
        "before": '    if not (jd.raw_text or "").strip() and not jd.must_have and not jd.tech_stack:',
        "after": "    if False:",
        "test": T,
        "case": "TestOptimizeFlow.test_empty_jd_returns_original_without_calling_model",
    },
    # ---------------- 字段补齐 ----------------
    {
        "id": "M18",
        "desc": "removed_keywords 读错键 → 服务端有、桌面缺的字段又丢了",
        "file": RO,
        "before": '    removed_keywords = _str_list(payload.get("removed_keywords"))',
        "after": '    removed_keywords = _str_list(payload.get("removed"))',
        "test": T,
        "case": "TestOptimizeFlow.test_happy_path_shape",
    },
    # ---------------- 一页预算（三刀裁剪） ----------------
    {
        "id": "M15",
        "desc": "精简结果不被采纳 → 白花一次调用，超长照旧",
        "file": RO,
        "before": '                if cond_preservation["passed"]:\n                    optimized = condensed',
        "after": '                if cond_preservation["passed"]:\n                    optimized = optimized',
        "test": T,
        "case": "TestCondense.test_oversize_triggers_condense_and_adopts_result",
    },
    # ---------------- 落库（写错表 / 丢结构化） ----------------
    {
        "id": "M8",
        "desc": "落点写错实体 → 存了却看不到，而且永远同步不上去",
        "file": RO,
        "before": '    seq = local_store.save("resumes", "create", item_id, item)',
        "after": '    seq = local_store.save("resumes_wrong", "create", item_id, item)',
        "test": T,
        "case": "TestSaveOptimized.test_writes_new_version_into_local_store",
    },
    {
        "id": "M9",
        "desc": "不落 structured → 优化稿导出退化成纯文本（丢标题行/bullet）",
        "file": RO,
        "before": '        "structured": structured,',
        "after": '        "structured": None,',
        "test": T,
        "case": "TestSaveOptimized.test_writes_new_version_into_local_store",
    },
    # ---------------- JD 需求 → 提示词 ----------------
    {
        "id": "M10",
        "desc": "tech_stack 只认 list → dict 形状的 JD 整个技术栈静默丢失",
        "file": RO,
        "before": "    out: List[str] = []\n    if isinstance(value, dict):",
        "after": "    out: List[str] = []\n    if False:",
        "test": T,
        "case": "TestJdRequirement.test_tech_stack_dict_is_flattened",
    },
    {
        "id": "M11",
        "desc": "JD 原文被截断到 600 字 → 后半段的必备要求永远喂不进模型",
        "file": MA,
        "before": '            parts.append("\\nJD 原文：\\n" + self.raw_text)',
        "after": '            parts.append("\\nJD 原文：\\n" + self.raw_text[:600])',
        "test": T,
        "case": "TestJdRequirement.test_full_jd_text_is_not_truncated",
    },
    # ---------------- 本地 ATS 前后对比 ----------------
    {
        "id": "M12",
        "desc": "不再本地算 ATS → 前后对比卡整块消失（回落到「什么都看不到」）",
        "file": RO,
        "before": '        result["ats"] = ats_core.check_ats_compatibility(original, req_text)',
        "after": '        result["ats"] = None',
        "test": T,
        "case": "TestAts.test_ats_before_and_after_computed_locally",
    },
    # ---------------- 提示词同源 ----------------
    {
        "id": "M13",
        "desc": "优化提示词丢掉服务端条款（量化梯度）",
        "file": PR,
        "before": "5. 量化梯度（原文没有数字时按序退让，绝不编造）：",
        "after": "5. （已移除）：",
        "test": T,
        "case": "TestPromptParity.test_server_terms_present",
    },
    # ---------------- 本地版本链（新建的版本要看得见、回得去） ----------------
    {
        "id": "M19",
        "desc": "忽略 parent_id → 靠父链表达的版本关系全断（本地记录大多没有 document_id）",
        "file": AC,
        "before": '    path.append(iid)\n    pid = str(item.get("parent_id") or "")',
        "after": '    path.append(iid)\n    pid = ""',
        "test": T,
        "case": "TestLocalVersionChain.test_parent_cycle_does_not_hang",
    },
    {
        "id": "M20",
        "desc": "版本列表不并本地链 → 刚优化完切到「历史版本」是一片空白（云端 404）",
        "file": AC,
        "before": "    for it in _local_chain(resume_id):",
        "after": "    for it in []:",
        "test": T,
        "case": "TestLocalVersionChain.test_cloud_down_still_lists_local_chain",
    },
    {
        "id": "M21",
        "desc": "回滚不拒绝跨文档 → 把另一份简历的版本覆盖进来",
        "file": AC,
        "before": '            raise ApiError("所选版本不属于同一份简历")',
        "after": "            pass",
        "test": T,
        "case": "TestLocalVersionChain.test_rollback_rejects_cross_document",
    },
    {
        "id": "M22",
        "desc": "回滚不记 rolled_back_from → 版本历史里看不出这一版是从哪恢复来的",
        "file": AC,
        "before": '        parsed["rolled_back_from"] = int(target.get("version") or 0)',
        "after": '        parsed.pop("rolled_back_from", None)',
        "test": T,
        "case": "TestLocalVersionChain.test_rollback_appends_new_highest_version",
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
