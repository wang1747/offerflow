#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""阶段 1 本地判据层（sufficiency / skill_keywords / quality / page_fit / fidelity）
的**反向对照**。

为什么必须有这一步：全绿的用例只说明「当前代码能过它」，不说明「换个错实现
它会拦下来」。判据写歪了（看错键、断言方向写反、压根没覆盖那条分支）时
用例照样全绿 —— 假绿比没写用例更危险。

做法：对生产源码做**精确字符串替换**（并校验锚点唯一）→ 跑指定用例 →
**无论结果都还原**。

⚠ 读写都必须 ``newline=""``：仓库约定 LF、``.gitattributes`` 是 ``* -text``、
打包产物按字节 sha256 校验。用默认文本模式回写会翻成 CRLF，产物哈希跟着变。

运行（必须用项目 venv）：.venv/Scripts/python.exe tests/_mutate_ai_judges_check.py
"""

import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
CORE = os.path.join(ROOT, "offerflow_desktop", "core")

SUF = os.path.join(CORE, "sufficiency.py")
SKW = os.path.join(CORE, "skill_keywords.py")
QUA = os.path.join(CORE, "quality.py")
PGF = os.path.join(CORE, "page_fit.py")
FID = os.path.join(CORE, "fidelity.py")

T = "test_ai_local_judges.py"

MUTATIONS = [
    # ---------------- sufficiency ----------------
    {
        "id": "M1",
        "desc": "sufficient 档的目标字数 800 改成 900（动态字数目标失效）",
        "file": SUF,
        "before": '"sufficient": {"target_length": 800, "min_length": 700},',
        "after": '"sufficient": {"target_length": 900, "min_length": 700},',
        "test": T,
        "case": "TestSufficiency.test_full_input_is_sufficient",
    },
    {
        "id": "M2",
        "desc": "缺技能时不再记 gap（前端拿不到「该补什么」）",
        "file": SUF,
        "before": '        gaps.append("缺少技能")',
        "after": '        pass',
        "test": T,
        "case": "TestSufficiency.test_missing_skill_is_named_in_gaps",
    },
    # ---------------- skill_keywords ----------------
    {
        "id": "M3",
        "desc": "tech 方向的技能表与 JOB_TEMPLATES 漂移（单份数据两处存）",
        "file": SKW,
        "before": '    "tech": ["Python", "Java", "Go", "MySQL", "Redis", "Docker", "Kubernetes",',
        "after": '    "tech": ["Python", "Java", "Go", "MySQL", "Redis", "Docker", "K8s",',
        "test": T,
        "case": "TestSkillKeywords.test_direction_skills_equal_job_templates",
    },
    {
        "id": "M4",
        "desc": "只认英文 key，不认中文标签（服务端口径 → 恒返回空）",
        "file": SKW,
        "before": "    if d in _LABEL_TO_KEY:\n        return _LABEL_TO_KEY[d]",
        "after": "    if False:\n        return _LABEL_TO_KEY[d]",
        "test": T,
        "case": "TestSkillKeywords.test_top_keywords_by_chinese_label",
    },
    # ---------------- quality ----------------
    {
        "id": "M5",
        "desc": "权重之和不再是 100（总分口径被破坏）",
        "file": QUA,
        "before": "W_VERB = 18",
        "after": "W_VERB = 19",
        "test": T,
        "case": "TestQuality.test_weights_sum_to_100",
    },
    {
        "id": "M6",
        "desc": "弱动词开头不再记硬违规（重试闭环的门槛被打掉）",
        "file": QUA,
        "before": "    if weak_bullets:\n"
                  '        hard.append(f"{len(weak_bullets)} 条要点弱动词开头")',
        "after": "    if False:\n"
                 '        hard.append(f"{len(weak_bullets)} 条要点弱动词开头")',
        "test": T,
        "case": "TestQuality.test_weak_verb_fails_hard",
    },
    {
        "id": "M7",
        "desc": "重写反馈里丢掉「事实红线」（模型失去不许编造的约束）",
        "file": QUA,
        "before": '            "【事实红线（优先级高于下面所有质量问题）】任何数字（百分比/倍数/次数/规模/客户数/耗时）"',
        "after": '            "请针对性重写："',
        "test": T,
        "case": "TestQuality.test_feedback_hint_carries_fact_redline",
    },
    {
        "id": "M8",
        "desc": "⚠️ 弱动词词表加词 → 与服务的同源对照必须变红（证明同源层不是空靶）",
        "file": QUA,
        "before": '    "处理", "使用", "维护", "支持", "帮忙", "跟进", "学习", "了解", "熟悉", "掌握",',
        "after": '    "处理", "使用", "维护", "支持", "帮忙", "跟进", "学习", "了解", "熟悉", "掌握", "撰写",',
        "test": T,
        "case": "TestServerParity.test_quality_weak_verbs_identical",
    },
    {
        "id": "M13",
        "desc": "字数不足不再记为硬违规（重试门槛被打掉）",
        "file": QUA,
        "before": '    if len((resume_text or "").strip()) < LENGTH_MIN:\n'
                  '        hard.append(f"字数不足 {LENGTH_MIN}")',
        "after": '    if False:\n'
                 '        hard.append(f"字数不足 {LENGTH_MIN}")',
        "test": T,
        "case": "TestQuality.test_short_text_fails_hard",
    },
    # ---------------- page_fit ----------------
    {
        "id": "M9",
        "desc": "⚠️ 经历标题改回服务端写法（与桌面 render 不一致 → 块匹配静默退化）",
        "file": PGF,
        "before": '    return f"{header}｜{period}" if (header and period) else (header or period)',
        "after": '    return f"{header}，{period}" if (header and period) else (header or period)',
        "test": T,
        "case": "TestPageFit.test_header_matches_render_exactly",
    },
    {
        "id": "M10",
        "desc": "退化守卫失效（要点全对不上时仍产出残缺简历）",
        "file": PGF,
        "before": "    if base_bullet_total and kept_bullet_total == 0:\n        return base",
        "after": "    if False:\n        return base",
        "test": T,
        "case": "TestPageFit.test_degenerate_condensation_falls_back_to_base",
    },
    {
        "id": "M11",
        "desc": "技能不再做交集（精简稿里没有的技能也留下 → 编造）",
        "file": PGF,
        "before": '    new["skills"] = keep_short_texts(list(base.get("skills") or []), condensed)',
        "after": '    new["skills"] = list(base.get("skills") or [])',
        "test": T,
        "case": "TestPageFit.test_skills_only_shrink",
    },
    # ---------------- fidelity ----------------
    {
        "id": "M12",
        "desc": "可溯源数字集合恒为空（合法数字会被误删）",
        "file": FID,
        "before": "    return {span[3] for span in _number_spans(normalized)}",
        "after": "    return set()",
        "test": T,
        "case": "TestFidelity.test_canonical_numbers_normalizes_variants",
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
