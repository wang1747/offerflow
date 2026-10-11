#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""反向对照：把「已修的缺陷」逐条放回去，确认对应判据真的变红。

为什么必须做：判据可能因为**取值取错键**而恒真（这正是 D1 的成因），
那样「全绿」什么都不能证明。这里每个变异都精确替换一处真缺陷，
并断言对应用例失败；变异没被拦下 → 该判据不可信。

读写一律 ``newline=""``：Windows 下默认换行转换会把文件写成 CRLF，
替换锚点（含 LF）就对不上了。

运行：python tests/_mutate_fix2_check.py
"""

import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = os.path.join(ROOT, ".venv", "Scripts", "python.exe")

GUARD = os.path.join(ROOT, "offerflow_desktop", "core", "generation_guard.py")
GEN_PAGE = os.path.join(ROOT, "offerflow_desktop", "ui", "pages", "resume_generate.py")
OPT_PAGE = os.path.join(ROOT, "offerflow_desktop", "ui", "pages", "resume_optimize.py")
SERVICES = os.path.join(ROOT, "offerflow_desktop", "services", "resume_generation.py")

TEST_FILE = "tests/test_resume_fix_round2.py"

# (名称, 文件, 原片段, 变异后片段, 期望变红的用例)
MUTATIONS = [
    (
        "D1 取值退回「只认 left」",
        GUARD,
        """    left = str(exp.get("left") or "").strip()
    if left:
        return left
    return " ".join(
        part for part in (str(exp.get("name") or "").strip(),
                          str(exp.get("role") or "").strip()) if part
    )""",
        """    return str(exp.get("left") or "").strip()""",
        ["TestRiskGuardOnGeneratedShape.test_outsource_label_fires_on_name_role",
         "TestRiskGuardOnGeneratedShape.test_toy_project_fires_on_name_role"],
    ),
    (
        "D3 历史回读退回只认 quality",
        GEN_PAGE,
        '"review": g.get("quality") or g.get("review") or {},',
        '"review": g.get("quality") or {},',
        ["TestHistoryReviewKey.test_reads_review_key"],
    ),
    (
        "D2 revalidate 退回整体替换",
        GEN_PAGE,
        """        merged = dict(self._current_result)
        merged.update(fresh)
        self._current_result = merged""",
        """        self._current_result = fresh""",
        ["TestRevalidateMerge.test_refresh_after_edit_keeps_metadata",
         "TestRevalidateMerge.test_variants_path_keeps_metadata"],
    ),
    (
        "D5 比例格式化退回「None 当 0」",
        OPT_PAGE,
        """    try:
        return f"{float(value):.0%}"
    except (TypeError, ValueError):
        return "—\"""",
        """    return f"{float(value or 0):.0%}\"""",
        ["TestOptimizePercent.test_pct_helper"],
    ),
    (
        "D6 收集时退回「无脑全收卡片」",
        GEN_PAGE,
        """            "education": [d for d in (c.data() for c in self._edu_cards)
                          if _edu_filled(d)],""",
        """            "education": [c.data() for c in self._edu_cards],""",
        ["TestEmptyCardValidation.test_blank_form_collects_nothing",
         "TestEmptyCardValidation.test_blank_form_blocks_generate"],
    ),
    (
        "D7 守门退回「完成时刻的 JD」",
        GEN_PAGE,
        'getattr(self, "_last_jd", "") or "")',
        'self.jd_edit.toPlainText().strip())',
        ["TestFrozenJd.test_on_generated_uses_frozen_jd"],
    ),
    (
        "D8a guard_text 退回「不传事实来源」",
        SERVICES,
        '        source_text=source_text or "",',
        '        source_text="",',
        ["TestGuardText.test_flags_unsourced_numbers"],
    ),
    (
        "D8b 优化页退回「不跑守门层」",
        OPT_PAGE,
        """        guard = rg.guard_text(
            result.get("optimized_text", ""),
            (self._resume or {}).get("raw_text") or "",
            (self._jd or {}).get("raw_text") or "",
        )""",
        """        guard = {"score": 100, "passed": True, "issues": []}""",
        ["TestGuardText.test_optimize_page_renders_guard_section"],
    ),
]

# 「接线级」变异：目标不是某个函数的取值，而是**页面有没有真的把守门层接上**。
# 对应的判据是 AST 扫描（test_generation_guard.py / test_layout_options.py），
# 所以 tests 里直接写测试脚本路径 —— 整份脚本变红才算拦下。
WIRING_MUTATIONS = [
    (
        "D9 守门重算退回「不传事实来源」",
        GEN_PAGE,
        "                              source_text=self._source_text())",
        "                              )",
        ["tests/test_generation_guard.py"],
    ),
    (
        "D9b 改排序/切版式后不再重算（_refresh_after_edit 空转）",
        GEN_PAGE,
        """    def _refresh_after_edit(self) -> None:
        \"\"\"结构改动后重新渲染与评分（不调 LLM）。\"\"\"
        self._revalidate_current()""",
        """    def _refresh_after_edit(self) -> None:
        \"\"\"结构改动后重新渲染与评分（不调 LLM）。\"\"\"
        return""",
        ["tests/test_generation_guard.py", "tests/test_layout_options.py"],
    ),
    (
        "D9c 换候选写法后不再重算（_on_variants 空转）",
        GEN_PAGE,
        """            # 重新渲染评分（不调 LLM）；合并保留 id/version/tips 等元数据
            self._revalidate_current()""",
        """            # 重新渲染评分（不调 LLM）；合并保留 id/version/tips 等元数据
            return""",
        ["tests/test_generation_guard.py", "tests/test_layout_options.py"],
    ),
    (
        "D10 重算漏传版式（ATS 分数按旧设置算）",
        GEN_PAGE,
        "                              template=self._effective_template(),\n",
        "",
        ["tests/test_layout_options.py"],
    ),
    (
        "D10b 重算漏传模块顺序",
        GEN_PAGE,
        "                              section_order=self._order,\n",
        "",
        ["tests/test_layout_options.py"],
    ),
]

# 变异后改用别的方式验证的项：构造期审计（D4）
SCRIPT_MUTATION = (
    "D4 构造期重新发网络请求（审计应报红）",
    OPT_PAGE,
    """        self.resume_hint.setVisible(True)
        self.optimize_btn.setEnabled(False)""",
    """        self.resume_hint.setVisible(True)
        self.optimize_btn.setEnabled(False)
        self._refresh()""",
    "tests/test_ctor_async_audit.py",
)


def _read(path):
    with open(path, "r", encoding="utf-8", newline="") as fh:
        return fh.read()


def _write(path, text):
    with open(path, "w", encoding="utf-8", newline="") as fh:
        fh.write(text)


def _run(cmd):
    env = dict(os.environ)
    env["QT_QPA_PLATFORM"] = "offscreen"
    return subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, env=env)


def _case_cmd(target):
    """一个「判据目标」怎么跑：

    * ``tests/xxx.py``  → 整份脚本跑一遍（AST 扫描 / unittest 文件都是这样）；
    * ``TestX.test_y``  → ``test_resume_fix_round2.py`` 里的单个用例
      （按用例粒度跑才能证明**是它**变红，而不是同文件别处连带炸了）。
    """
    if target.endswith(".py"):
        return [PY, target]
    return [PY, TEST_FILE, target]


def _all_mutations():
    return MUTATIONS + WIRING_MUTATIONS


def _check_anchors():
    """先验证每个锚点在文件里唯一 —— 锚点不唯一说明我改的代码和预期不一致。"""
    ok = True
    items = [(n, p, o) for n, p, o, _w, _t in _all_mutations()]
    items.append((SCRIPT_MUTATION[0], SCRIPT_MUTATION[1], SCRIPT_MUTATION[2]))
    for name, path, old in items:
        count = _read(path).count(old)
        if count != 1:
            print("!! 锚点不唯一（出现 %d 次）：%s" % (count, name))
            ok = False
    return ok


def main():
    if not os.path.exists(PY):
        sys.exit("找不到 venv python: %s" % PY)
    if not _check_anchors():
        sys.exit("锚点校验失败，先修脚本")

    # 基线：未变异时每一个判据目标都必须绿（否则「变红」没有意义）
    targets = []
    for _n, _p, _o, _w, tests in _all_mutations():
        for t in tests:
            if t not in targets:
                targets.append(t)
    for target in targets:
        base = _run(_case_cmd(target))
        if base.returncode != 0:
            print(base.stdout[-3000:])
            sys.exit("基线不绿（%s），先修实现" % target)

    failures = []
    for name, path, old, new, tests in _all_mutations():
        original = _read(path)
        try:
            _write(path, original.replace(old, new, 1))
            if not tests:
                print("跳过（无对应用例）：%s" % name)
                failures.append(name)
                continue
            red = []
            for tid in tests:
                proc = _run(_case_cmd(tid))
                if proc.returncode != 0:
                    red.append(tid)
            if len(red) == len(tests):
                print("✓ 被拦下：%s（%d 个用例变红）" % (name, len(red)))
            else:
                missed = [t for t in tests if t not in red]
                print("✗ 漏报：%s —— 变异后仍绿：%s" % (name, missed))
                failures.append(name)
        finally:
            _write(path, original)

    # D4 单独跑：变异目标是「构造期审计」这个脚本，不是用例
    name, path, old, new, script = SCRIPT_MUTATION
    original = _read(path)
    try:
        _write(path, original.replace(old, new, 1))
        proc = _run([PY, script])
        if proc.returncode != 0:
            print("✓ 被拦下：%s" % name)
        else:
            print("✗ 漏报：%s —— 构造期发了请求但审计仍然放行" % name)
            failures.append(name)
    finally:
        _write(path, original)

    print()
    total = len(MUTATIONS) + len(WIRING_MUTATIONS) + 1
    if failures:
        sys.exit("反向对照失败 %d 项：%s" % (len(failures), failures))
    print("反向对照通过：%d 处变异全部被判据拦下" % total)


if __name__ == "__main__":
    main()
