#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""阶段 2 本地生成主流程（`services/resume_generation.generate_resume` + `prompts`）
的**反向对照**。

为什么必须有这一步：全绿的用例只说明「当前代码能过它」，不说明「换个错实现
它会拦下来」。护栏接歪了（忘传 edu_input、剔除了但没重渲染、fail-closed 那段
删了、hint 没带上）时用例照样全绿 —— 假绿比没写用例更危险。

做法：对生产源码做**精确字符串替换**（并校验锚点唯一）→ 跑指定用例 →
**无论结果都还原**。

⚠ 读写都必须 ``newline=""``：仓库约定 LF、``.gitattributes`` 是 ``* -text``、
打包产物按字节 sha256 校验。用默认文本模式回写会翻成 CRLF，产物哈希跟着变。

运行（必须用项目 venv、且 ``env -u PYTHONPATH`` 以避开沙箱 safe-delete shim）：
  env -u PYTHONPATH .venv/Scripts/python.exe tests/_mutate_ai_generate_check.py
"""

import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SERV = os.path.join(ROOT, "offerflow_desktop", "services")

RG = os.path.join(SERV, "resume_generation.py")   # 生成编排 + 护栏
PR = os.path.join(SERV, "prompts.py")             # 生成提示词

T = "test_ai_local_generate.py"

MUTATIONS = [
    # ---------------- 事实回填（防编造学校 / 公司 / 姓名 / 联系方式） ----------------
    {
        "id": "M1",
        "desc": "姓名不再回填 → 模型编造的姓名进产物",
        "file": RG,
        "before": '        name = _line(identity_input.get("name")) or name',
        "after": '        name = name',
        "test": T,
        "case": "TestFactBackfill.test_identity_uses_user_input",
    },
    {
        "id": "M2",
        "desc": "教育经历不再回填 → 模型编造的学校 / 学历进产物",
        "file": RG,
        "before": '    education = edu_input if edu_input is not None else _dict_list(payload.get("education"))',
        "after": '    education = _dict_list(payload.get("education"))',
        "test": T,
        "case": "TestFactBackfill.test_education_uses_user_input",
    },
    {
        "id": "M3",
        "desc": "经历的事实字段不再回填 → 模型编造的项目 / 公司名进产物",
        "file": RG,
        "before": '            exp["name"] = src.get("name", "")',
        "after": '            exp["name"] = exp.get("name", "")',
        "test": T,
        "case": "TestFactBackfill.test_experience_facts_use_user_input",
    },
    {
        "id": "M13",
        "desc": "经历条数不符时不再报错 → 用户的整段经历被静默丢弃",
        "file": RG,
        "before": '        return {"error": "生成结果丢失了部分项目或实习经历，请重试"}',
        "after": '        pass',
        "test": T,
        "case": "TestFactBackfill.test_experience_count_mismatch_errors",
    },
    # ---------------- 编造内容剔除 ----------------
    {
        "id": "M4",
        "desc": "编造的技能不再剔除（用户没写的技能照样落库）",
        "file": RG,
        "before": '        structured["skills"] = [s for s in (structured.get("skills") or [])\n'
                  '                                if _normalize_fact(str(s)) not in drop]',
        "after": '        structured["skills"] = list(structured.get("skills") or [])',
        "test": T,
        "case": "TestFabricatedFactsStripped.test_added_skill_is_removed",
    },
    {
        "id": "M5",
        "desc": "删技能后不同步清理 skill_groups → 渲染里「删了还显示」",
        "file": RG,
        "before": "        _prune_skill_groups(structured)",
        "after": "        pass",
        "test": T,
        "case": "TestFabricatedFactsStripped.test_skill_groups_pruned_too",
    },
    # ---------------- 数字确定性兜底 ----------------
    {
        "id": "M6",
        "desc": "数字净化不再执行 → 模型编造的百分比 / 规模数字进产物",
        "file": RG,
        "before": "    _, removed = bullet_fidelity.sanitize_structured(structured, source_text)",
        "after": "    removed = []",
        "test": T,
        "case": "TestNumberSanitize.test_fabricated_percent_is_stripped",
    },
    # ---------------- 一页硬约束 fail-closed ----------------
    {
        "id": "M7",
        "desc": "一页约束不再 fail closed → 「界面一页、导出两页」放行",
        "file": RG,
        "before": '    if page_fit.is_too_long(result.get("resume_text") or ""):\n'
                  '        result["error"] = (',
        "after": '    if False:\n'
                 '        result["error"] = (',
        "test": T,
        "case": "TestOnePageFailClosed.test_oversize_without_condense_fails_closed",
    },
    # ---------------- 动态字数 ----------------
    {
        "id": "M8",
        "desc": "字数目标退回硬编码 800（动态判据等于没接上）",
        "file": RG,
        "before": '        f"【篇幅要求】全文控制在 {target_length} 字左右，误差 ±50 字。"',
        "after": '        f"【篇幅要求】全文控制在 800 字左右，误差 ±50 字。"',
        "test": T,
        "case": "TestDynamicLength.test_target_length_reaches_user_prompt",
    },
    # ---------------- 质量重试闭环 ----------------
    {
        "id": "M9",
        "desc": "质量不达标不再重写 → 评分器变成摆设",
        "file": RG,
        "before": "    if not quality.passed:\n"
                  '        logger.warning("简历质量不达标(%s 分)，触发质量重试", quality.total_score)',
        "after": "    if False:\n"
                 '        logger.warning("简历质量不达标(%s 分)，触发质量重试", quality.total_score)',
        "test": T,
        "case": "TestQualityRetry.test_quality_retry_fires_with_feedback_hint",
    },
    # ---------------- 语义评分 ----------------
    {
        "id": "M10",
        "desc": "语义评分不再接入（AI 那一层评分整块丢失）",
        "file": RG,
        "before": '        result["semantic"] = _semantic_gate(result, jd_prompt)',
        "after": '        result["semantic"] = {}',
        "test": T,
        "case": "TestSemanticGate.test_semantic_scores_attached",
    },
    # ---------------- 字段级保真（学校 / 公司） ----------------
    {
        "id": "M11",
        "desc": "学校编造不再检出（本轮刚从服务端补齐的那条判据）",
        "file": RG,
        "before": '        if not input_schools or not any(sn in s or s in sn for s in input_schools):\n'
                  '            added_schools.append(edu.get("school") if isinstance(edu, dict) else edu)',
        "after": '        if False:\n'
                 '            added_schools.append(edu.get("school") if isinstance(edu, dict) else edu)',
        "test": T,
        "case": "TestFactBackfill.test_fidelity_catches_fabrication_without_backfill",
    },
    # ---------------- 提示词同源 ----------------
    {
        "id": "M12",
        "desc": "生成提示词丢掉服务端「决策-权衡写法」条款（与最新服务端脱节）",
        "file": PR,
        "before": "决策-权衡写法（该条经历里存在技术选型、架构权衡或复杂约束时优先用）：",
        "after": "选型写法：",
        "test": T,
        "case": "TestPromptParity.test_desktop_prompt_has_server_clauses",
    },
    # ---------------- 落本地主存 ----------------
    {
        "id": "M14",
        "desc": "落点写错表（resumes → applications）→ 「存了却看不到」",
        "file": RG,
        "before": '    seq = local_store.save("resumes", "create", item_id, item)',
        "after": '    seq = local_store.save("applications", "create", item_id, item)',
        "test": T,
        "case": "TestSaveGenerated.test_roundtrip_visible_to_list_items",
    },
    # ---------------- 候选变体数字净化 ----------------
    {
        "id": "M15",
        "desc": "候选不再做数字净化（唯一绕过生成/重写两关的路径重新变成零护栏）",
        "file": RG,
        "before": "        fixed, removed = _sanitize_candidate(candidate, source_text)",
        "after": "        fixed, removed = candidate, []",
        "test": T,
        "case": "TestVariantSanitize.test_fabricated_number_in_candidate_is_stripped",
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
