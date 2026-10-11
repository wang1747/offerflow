#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""复用闭环的反向对照：逐条砍掉机制，验证 `test_ai_local_retrieval_closure.py` 变红。

为什么必须做：本文件验证的几条（成果分流 / 质量闸门 / 白名单 / 回声防护 /
自动入池 / 重建不重复）**都可能被写成一个恒返回「通过」的假实现**。
不逐条砍一刀，就分不清「机制真的在起作用」还是「判据本身是空靶」。

跑法：python tests/_mutate_retrieval_closure_check.py
"""

import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PKG = os.path.join(ROOT, "offerflow_desktop")
TESTS = os.path.join(ROOT, "tests")

T = "test_ai_local_retrieval_closure.py"

P_IDX = os.path.join(PKG, "local_ai", "index.py")
P_STORE = os.path.join(PKG, "local_ai", "store.py")
P_RET = os.path.join(PKG, "local_ai", "retrieve.py")
P_RG = os.path.join(PKG, "services", "resume_generation.py")
P_RO = os.path.join(PKG, "services", "resume_optimizer.py")
P_CMP = os.path.join(PKG, "ui", "components.py")

MUTATIONS = [
    # ---- 来源分流 ----
    {
        "id": "M1", "desc": "成果不分流（一律按材料池入索引）",
        "file": P_IDX,
        "before": '    s = str((item or {}).get("source") or "").strip().lower()\n'
                  '    if _is_work_source(s):\n'
                  '        return s if _work_quality_ok(item) else "resume"\n'
                  '    return "resume"',
        "after": '    return default',
        "test": T, "case": "TestWorkSourceRouting.test_generated_and_optimized_go_to_work_pool",
    },
    {
        "id": "M2", "desc": "去掉质量闸门（低质成果照样进复用池）",
        "file": P_IDX,
        "before": '        return s if _work_quality_ok(item) else "resume"',
        "after": '        return s',
        "test": T, "case": "TestWorkSourceRouting.test_low_quality_work_is_demoted",
    },
    # ---- 白名单 ----
    {
        "id": "M3", "desc": "白名单退化：source 列表被当成单元素集合（抛异常→返回空）",
        "file": P_STORE,
        "before": '                want = {source} if isinstance(source, str) else set(source)',
        "after": '                want = {source}',
        "test": T, "case": "TestSourceWhitelist.test_whitelist_list_excludes_material_pool",
    },
    # ---- 回声防护 ----
    {
        "id": "M4", "desc": "去掉回声防护（与自己几乎同分的块照样当参考）",
        "file": P_RET,
        "before": '        if _is_work_source(r.get("source")) and score >= ECHO_SCORE_CEILING:\n'
                  '            echo_dropped += 1\n'
                  '            continue',
        "after": '        if False:\n'
                 '            echo_dropped += 1\n'
                 '            continue',
        "test": T, "case": "TestEchoGuard.test_identical_content_is_dropped_as_echo",
    },
    # ---- 双池召回（生成侧）----
    {
        "id": "M5", "desc": "生成链路不取成果池（复用闭环断一半）",
        "file": P_RG,
        "before": '        work_res = retrieve.retrieve_context(\n'
                  '            query, item_type="resume", top_k=work_top_k, sources=list(WORK_SOURCES))',
        "after": '        work_res = {"text": "", "hits": 0, "echo_dropped": 0}',
        "test": T, "case": "TestLoadReferenceTwoPools.test_works_pool_joins_recall_after_save",
    },
    {
        "id": "M6", "desc": "召回轨迹不记 work_hits（界面永远显示 0 条成果）",
        "file": P_RG,
        "before": '            trace["work_hits"] = int(work_res.get("hits") or 0)',
        "after": '            trace["work_hits"] = 0',
        "test": T, "case": "TestLoadReferenceTwoPools.test_works_pool_joins_recall_after_save",
    },
    {
        "id": "M7", "desc": "生成链路不取 corpus（只剩自己的成果）",
        "file": P_RG,
        "before": '        corpus_res = retrieve.retrieve_context(\n'
                  '            query, item_type="resume", top_k=top_k, sources=["corpus"])',
        "after": '        corpus_res = {"text": "", "hits": 0, "echo_dropped": 0}',
        "test": T, "case": "TestLoadReferenceTwoPools.test_generation_reference_has_trace",
    },
    {
        "id": "M8", "desc": "成果块不标注来历（模型会当成第三方资料照抄事实）",
        "file": P_RG,
        "before": '            parts.append(\n'
                  '                "【你自己过去生成/优化过的高分简历写法（只借鉴结构与表达，"\n'
                  '                "不得复用其中的具体事实）】\\n" + work_res["text"])',
        "after": '            parts.append(work_res["text"])',
        "test": T, "case": "TestLoadReferenceTwoPools.test_work_block_is_labeled_in_prompt_text",
    },
    # ---- 自动入池 ----
    {
        "id": "M9", "desc": "生成结果落库后不进索引（要等下次重建）",
        "file": P_RG,
        "before": '    _index_completed_work(item)   # 立刻进复用池，不等下次重建',
        "after": '    pass',
        "test": T, "case": "TestAutoIndexOnSave.test_save_generated_indexes_immediately",
    },
    {
        "id": "M10", "desc": "优化结果落库后不进索引（要等下次重建）",
        "file": P_RO,
        "before": '    _index_completed_work(item)   # 立刻进复用池，不等下次重建',
        "after": '    pass',
        "test": T, "case": "TestAutoIndexOnSave.test_save_optimized_indexes_immediately",
    },
    {
        "id": "M11", "desc": "落库 payload 不带质量报告（闸门拿不到分数）",
        "file": P_RG,
        "before": '            "quality": dict(result.get("quality") or {}),',
        "after": '            "quality": {},',
        "test": T, "case": "TestAutoIndexOnSave.test_low_quality_work_not_in_pool",
    },
    # ---- 重建合并 ----
    {
        "id": "M12", "desc": "重建时不识别成果 id（同一条会出两份：resume + generated）",
        "file": P_IDX,
        "before": '    work_ids = _collect_work_ids()',
        "after": '    work_ids = {}',
        "test": T, "case": "TestRebuildMergesWorks.test_rebuild_keeps_works_once",
    },
    {
        "id": "M13", "desc": "重建快照不跳过成果 id（成果被快照以 resume 覆盖回去）",
        "file": P_IDX,
        "before": '        recs = _records_from_snapshot(name, snap, skip_ids=work_ids.get(name))',
        "after": '        recs = _records_from_snapshot(name, snap)',
        "test": T, "case": "TestRebuildMergesWorks.test_rebuild_does_not_let_snapshot_reclaim_work",
    },
    # ---- 界面文案 ----
    {
        "id": "M14", "desc": "没召回到也照样说「参考了」（界面上不说实话）",
        "file": P_CMP,
        "before": '    if not corpus_n and not work_n:\n'
                  '        err = str(trace.get("error") or "").strip()\n'
                  '        if err:\n'
                  '            return "📚 本次未取到参考语料（%s）—— 生成结果不受影响。" % err[:60]\n'
                  '        return ("📚 本次没有召回到参考语料（本地知识库可能尚未建立或已过期，"\n'
                  '                "可到「本地知识库」重建索引）—— 生成结果不受影响。")',
        "after": '    if False:\n'
                 '        pass',
        "test": T, "case": "TestRetrievalSummary.test_reports_empty_hit_honestly",
    },
]


def run_case(test: str, case: str) -> bool:
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)     # 沙箱下 PYTHONPATH 里的 shim 会引发原生崩溃
    env["QT_QPA_PLATFORM"] = "offscreen"
    proc = subprocess.run(
        [sys.executable, "-u", os.path.join(TESTS, test), case],
        cwd=ROOT, capture_output=True, text=True, env=env,
    )
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
                print("[%s] 锚点在文件里出现 %d 次，落点不确定：%s" % (num, n, desc))
                bad.append("%s 锚点不唯一" % num)
                continue
            if not run_case(mut["test"], mut["case"]):
                print("[%s] 基线就是红的，无法判断变异是否被拦住：%s" % (num, mut["case"]))
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
