#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P1（G8 薪资基准 / G9 调用链下钻 / G10 首页速览）判据的**反向对照**。

为什么必须有这一步：全绿的用例只说明「当前代码能过它」，不说明「换个错实现
它会拦下来」。判据写歪了（看错方法、断言方向写反、压根没覆盖那条分支）时
用例照样全绿 —— 假绿比没写用例更危险。

做法：对生产源码做**精确字符串替换**（并校验锚点唯一）→ 跑指定用例 →
**无论结果都还原**。

⚠ 读写都必须 ``newline=""``：仓库约定 LF、``.gitattributes`` 是 ``* -text``、
打包产物按字节 sha256 校验。用默认文本模式回写，Python 在 Windows 会把
``\\n`` 翻成 ``os.linesep``，整个文件变成 CRLF，产物哈希跟着变。

运行（必须用项目 venv）：.venv/Scripts/python.exe tests/_mutate_p1_check.py
"""

import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
PKG = os.path.join(ROOT, "offerflow_desktop")

API = os.path.join(PKG, "api_client.py")
SALARY = os.path.join(PKG, "ui", "pages", "salary.py")
COST = os.path.join(PKG, "ui", "pages", "cost.py")
DASH = os.path.join(PKG, "ui", "pages", "dashboard.py")
CHARTS = os.path.join(PKG, "ui", "charts.py")

T_SALARY = "test_salary_benchmark.py"
T_COST = "test_cost_trace_detail.py"
T_DASH = "test_dashboard_glance.py"

MUTATIONS = [
    # ---------------- G8：薪资基准刷新 / 导入 ----------------
    {
        "id": "M1",
        "desc": "导入校验只看字段在不在（空字段被放行，白跑一轮网络）",
        "file": SALARY,
        "before": '    missing = [k for k in BENCHMARK_REQUIRED_FIELDS if not parsed.get(k)]',
        "after": '    missing = [k for k in BENCHMARK_REQUIRED_FIELDS if k not in parsed]',
        "test": T_SALARY,
        "case": "TestParsePayload.test_empty_required_field_counts_as_missing",
    },
    {
        "id": "M2",
        "desc": "刷新假装成功（本地化后它必须固定拒绝，吞掉 = 用户以为行情刷新了）",
        "file": API,
        "before": '    raise ApiError("薪资基准已改为联网采集真实行情更新，'
                  '不再使用模型估算，请通过「导入」写入权威数据")',
        "after": "    return {}",
        "test": T_SALARY,
        "case": "TestApiClientLocalSemantics.test_refresh_is_hard_rejected",
    },
    {
        "id": "M3",
        "desc": "导入丢掉 data_year/source/note（来源与年份信息不进本机记录）",
        "file": API,
        "before": "        _bench_store.refresh(data_json, data_year=data_year, source=source, note=note,\n"
                  "                             report=report)",
        "after": '        _bench_store.refresh(data_json, data_year=data_year, source="", note="",\n'
                 "                             report=report)",
        "test": T_SALARY,
        "case": "TestApiClientLocalSemantics.test_import_writes_local_version_and_is_readable_back",
    },
    {
        "id": "M4",
        "desc": "基准换版后不清旧结果（旧行情算出的价格继续显示）",
        "file": SALARY,
        "before": "        self._set_bench_msg(msg)\n"
                  "        self._clear_reference_result()",
        "after": "        self._set_bench_msg(msg)",
        "test": T_SALARY,
        "case": "TestSalaryPageBenchmarkBar.test_import_success_uses_dialog_payload_and_says_local_only",
    },
    {
        "id": "M5",
        "desc": "导入失败不复位按钮（按钮永久置灰，用户没法重试）",
        "file": SALARY,
        "before": "        self.bench_import_btn.setEnabled(True)\n"
                  "        self._set_bench_msg(msg)",
        "after": "        self._set_bench_msg(msg)",
        "test": T_SALARY,
        "case": "TestSalaryPageBenchmarkBar.test_import_failure_reports_and_restores_button",
    },
    # ---------------- G9：成本调用链下钻 ----------------
    {
        "id": "M6",
        "desc": "费用未知当成 0（显示 ¥0.00，看着像免费）",
        "file": COST,
        "before": '            "未知" if cost is None else fmt_cost(float(cost)),',
        "after": "            fmt_cost(float(cost or 0)),",
        "test": T_COST,
        "case": "TestSpanRows.test_unknown_cost_says_unknown_not_zero",
    },
    {
        "id": "M7",
        "desc": "瀑布图丢掉起始偏移（条全从 0 起，看不出时序）",
        "file": CHARTS,
        "before": '            left = float(s.get("start_offset_ms") or 0) / total',
        "after": "            left = 0.0",
        "test": T_COST,
        "case": "TestWaterfallGeometry.test_left_offset_really_participates",
    },
    {
        "id": "M8",
        "desc": "点开行却不拉明细（永远停在「读取明细中…」）",
        "file": COST,
        "before": "        worker = LLMWorker(api_guard(lambda: api.get_trace_detail(trace_id)))",
        "after": "        return",
        "test": T_COST,
        "case": "TestCostPageDrilldown.test_clicking_a_row_fetches_detail_lazily",
    },
    {
        "id": "M9",
        "desc": "手风琴不关上一条（同时展开多条）",
        "file": COST,
        "before": "        prev = self._rows.get(self._expanded_id or \"\")\n"
                  "        if prev is not None and prev is not row:",
        "after": "        prev = self._rows.get(self._expanded_id or \"\")\n"
                 "        if False and prev is not None and prev is not row:",
        "test": T_COST,
        "case": "TestCostPageDrilldown.test_accordion_keeps_one_open",
    },
    # ---------------- G10：首页最近投递 + 匹配速览 ----------------
    {
        "id": "M10",
        "desc": "最近投递不截断（8 条全列，首页被撑长）",
        "file": DASH,
        "before": "        for app in apps[:RECENT_LIMIT]:",
        "after": "        for app in apps:",
        "test": T_DASH,
        "case": "TestRecentApplications.test_recent_is_truncated_to_limit",
    },
    {
        "id": "M11",
        "desc": "没有匹配记录时仍显示空卡（首页多出一块占位）",
        "file": DASH,
        "before": "        if not rankings:\n"
                  "            self.match_card.setVisible(False)\n"
                  "            return",
        "after": "        if False:\n"
                 "            self.match_card.setVisible(False)\n"
                 "            return",
        "test": T_DASH,
        "case": "TestMatchGlance.test_card_hidden_when_no_rankings",
    },
    {
        "id": "M12",
        "desc": "匹配分数写死（不再按记录取值）",
        "file": DASH,
        "before": '            rl.addWidget(score_badge(int(item.get("score") or 0)))',
        "after": "            rl.addWidget(score_badge(100))",
        "test": T_DASH,
        "case": "TestMatchGlance.test_score_bands_map_to_semantic_classes",
    },
    {
        "id": "M13",
        "desc": "构造期就拉匹配排行（页面销毁时回调摸空对象 → 静默崩退）",
        "file": DASH,
        "before": "        frame.setVisible(False)\n        return frame",
        "after": "        frame.setVisible(False)\n"
                 "        self._render_match(db.list_matches())\n"
                 "        return frame",
        "test": T_DASH,
        "case": "TestRecentApplications.test_ctor_shows_empty_state_without_fetching_matches",
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
