# -*- coding: utf-8 -*-
"""生成守门层测试：四条判据 + 反向对照 + 变异测试 + 活路径接线。

为什么这些用例长这样（三条来自错题本的约束）：

1. **每条判据都有正反两面**：正向把缺陷样本放进去必须报；反向把样本换成正常
   写法必须不报。只验正向的用例，写错了也看不出来（D-08）。
2. **末尾做变异测试**：把某个 guard 打坏（让它永远返回空），对应用例必须变红；
   恢复后必须变绿。这一步才分得清「用例在测真东西」和「用例在空转」（D-17）。
3. **接线断言走 AST**，不靠 grep 源码文本 —— 注释里写一句同样的话就能骗过
   grep（D-06）。

另外三条用例专门盯「守门层有没有真的挂到运行路径上」：桌面版生成走云端
（api_client），本地只有 revalidate 是活的。守门层若挂在 services.generate_resume
（无外部调用方）上，就是「有源码 ≠ 在运行」（E-09）。
"""
import ast
import sys
from pathlib import Path

# 仓库内定位：tests/ 的父目录即 desktop/（offerflow_desktop 包所在）。
_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

from offerflow_desktop.core import generation_guard as G          # noqa: E402
from offerflow_desktop.services import resume_generation as rg    # noqa: E402

FAILS = []


def check(name, cond):
    if not cond:
        FAILS.append(name)
    print("[%s] %s" % ("PASS" if cond else "FAIL", name))


def cats(report):
    return {i["category"] for i in report["issues"]}


def has(report, category, severity=None):
    for item in report["issues"]:
        if item["category"] == category and (severity is None
                                             or item["severity"] == severity):
            return True
    return False


# ============ 1. 编造检测 ============
SRC_NO_NUM = "求职意向：后端开发\n技能：Python、FastAPI"

r = G.check({"experiences": [{"left": "订单系统",
                              "bullets": ["优化查询，响应时间降低 40%，吞吐提升 3 倍"]}]},
            "订单系统\n优化查询，响应时间降低 40%，吞吐提升 3 倍",
            source_text=SRC_NO_NUM)
check("编造｜源里没有任何数字 → 报 high", has(r, "编造", "high"))

r = G.check({"experiences": [{"left": "订单系统",
                              "bullets": ["优化查询，响应时间降低 40%，吞吐提升 3 倍"]}]},
            "订单系统\n优化查询，响应时间降低 40%，吞吐提升 3 倍",
            source_text="订单系统：优化查询，响应时间降低 40%，吞吐提升 3 倍")
check("编造｜数字有出处 → 不报 high", not has(r, "编造", "high"))

r = G.check({"experiences": [{"left": "某科技有限公司 · 后端", "bullets": ["做检索"]}]},
            "某科技有限公司 · 后端\n做检索", source_text="求职意向：后端开发")
check("编造｜未见过的机构名 → 报", has(r, "编造"))

r = G.check({"experiences": [{"left": "某科技有限公司 · 后端", "bullets": ["做检索"]}]},
            "某科技有限公司 · 后端\n做检索", source_text="某科技有限公司 后端实习")
check("编造｜机构名来自用户输入 → 不报", not has(r, "编造"))

r = G.check({"experiences": [{"left": "项目A", "bullets": ["2021 年上线"]}]},
            "项目A\n2021 年上线", source_text="项目A：做检索")
check("编造｜源里没有的年份 → 报", has(r, "编造"))

r = G.check({"experiences": [{"left": "x", "bullets": ["y"]}]}, "x\ny", source_text="")
check("编造｜没有原始输入 → 降级为 low（不冒充通过）",
      has(r, "编造", "low") and not has(r, "编造", "high"))

# 硬事实丢失（复用 core/preservation 的抽取器，反方向的问题）
r = G.check({"experiences": [{"left": "项目A", "bullets": ["做检索"]}]},
            "项目A\n做检索",
            source_text="张三 zhang@example.com 138-0000-0000 项目A 做检索")
check("编造｜原始材料里的邮箱/电话丢了 → 报", has(r, "编造"))

# ============ 2. 中文求职风险 ============
r = G.check({"experiences": [{"left": "软通动力 · 后端开发", "bullets": ["做检索"]}]},
            "软通动力 · 后端开发\n做检索", source_text="软通动力 后端开发")
check("风险｜外包标签在标题行 → 报", has(r, "风险"))

r = G.check({"experiences": [{"left": "秒杀系统", "bullets": ["用 Redis 扣减库存"]}]},
            "秒杀系统\n用 Redis 扣减库存", source_text="秒杀系统 用 Redis 扣减库存")
check("风险｜玩具项目题材 → 报", has(r, "风险"))

r = G.check({"experiences": [{"left": "项目A", "bullets": ["用 Django 写后台"]}],
             "skills": ["精通 FastAPI"]},
            "项目A\n用 Django 写后台\n技能：精通 FastAPI",
            source_text="项目A 用 Django 写后台 技能 精通 FastAPI")
check("风险｜「精通」但经历里无证据 → 报", has(r, "风险"))

r = G.check({"experiences": [{"left": "项目A", "bullets": ["用 FastAPI 写接口"]}],
             "skills": ["精通 FastAPI"]},
            "项目A\n用 FastAPI 写接口\n技能：精通 FastAPI",
            source_text="项目A 用 FastAPI 写接口 技能 精通 FastAPI")
check("风险｜经历里有证据 → 不报", not has(r, "风险"))

# ============ 3. AI 腔 ============
r = G.check({"experiences": [{"left": "项目", "bullets": ["持续赋能业务，打通端到端闭环"]}]},
            "持续赋能业务，打通端到端闭环，形成方法论沉淀与抓手",
            source_text="项目：做检索")
check("AI腔｜黑话堆砌 → 报", has(r, "AI腔"))

r = G.check({"experiences": [{"left": "项目",
                              "bullets": ["用 Redis 缓存订单查询，P99 从 800ms 降到 120ms"]}]},
            "用 Redis 缓存订单查询，P99 从 800ms 降到 120ms",
            source_text="项目：用 Redis 缓存订单查询")
check("AI腔｜正常技术描述 → 不报", not has(r, "AI腔"))

# ============ 4. 量化降级建议 ============
NO_QUANT = ["负责后端接口开发与维护", "参与需求评审与联调", "协助排查线上问题"]
r = G.check({"experiences": [{"left": "项目A", "bullets": NO_QUANT}]},
            "\n".join(NO_QUANT), source_text=" ".join(NO_QUANT))
check("量化｜三条全无结果数据 → 报", has(r, "量化"))

QUANT = ["把 P99 从 800ms 降到 120ms", "支撑 5000 QPS", "覆盖率从 30% 提到 85%"]
r = G.check({"experiences": [{"left": "项目A", "bullets": QUANT}]},
            "\n".join(QUANT), source_text=" ".join(QUANT))
check("量化｜已量化 → 不报", not has(r, "量化"))

# ============ 5. 干净样本必须全绿 ============
CLEAN_SRC = ("张三\n求职意向：后端开发\n电话：138-0000-0000\n邮箱：zhang@example.com\n"
             "教育：某大学 计算机科学与技术\n项目：简历生成系统\n"
             "- 主导生成链路重构，把响应时间从 800ms 降到 200ms\n"
             "- 搭建向量检索，支撑 5000 QPS\n技能：Python、FastAPI、Redis")
CLEAN_GEN = ("张三\n电话：138-0000-0000 邮箱：zhang@example.com\n"
             "主导生成链路重构，把响应时间从 800ms 降到 200ms\n"
             "搭建向量检索，支撑 5000 QPS\n技能：Python、FastAPI、Redis")
r = G.check({"experiences": [{"left": "简历生成系统", "bullets": [
    "主导生成链路重构，把响应时间从 800ms 降到 200ms",
    "搭建向量检索，支撑 5000 QPS"]}],
    "skills": ["Python", "FastAPI", "Redis"]},
    CLEAN_GEN, source_text=CLEAN_SRC)
check("干净样本 → 一条都不报", len(r["issues"]) == 0)
check("干净样本 → passed=True", r["passed"] is True)

# ============ 6. 注册表与优先级 ============
check("注册表｜四个 guard 按优先级排序",
      G.GuardRegistry.names() == ["FabricationGuard", "RiskGuard", "AIToneGuard",
                                  "QuantificationGuard"])

# ============ 7. 变异测试（打坏 → 必须变红 → 恢复 → 变绿）============
MUT = {
    "编造": (lambda: has(G.check(
        {"experiences": [{"left": "订单系统", "bullets": ["响应时间降低 40%"]}]},
        "订单系统\n响应时间降低 40%", source_text=SRC_NO_NUM), "编造"),
        G.FabricationGuard),
    "风险": (lambda: has(G.check(
        {"experiences": [{"left": "软通动力 · 后端", "bullets": ["做检索"]}]},
        "软通动力 · 后端\n做检索", source_text="软通动力 后端"), "风险"),
        G.RiskGuard),
    "AI腔": (lambda: has(G.check(
        {"experiences": [{"left": "项目", "bullets": ["持续赋能，打通闭环"]}]},
        "持续赋能，打通闭环，形成方法论沉淀与抓手", source_text="项目：做检索"), "AI腔"),
        G.AIToneGuard),
    "量化": (lambda: has(G.check(
        {"experiences": [{"left": "项目A", "bullets": NO_QUANT}]},
        "\n".join(NO_QUANT), source_text=" ".join(NO_QUANT)), "量化"),
        G.QuantificationGuard),
}
for category, (probe, guard_cls) in MUT.items():
    before = probe()
    original = guard_cls.evaluate
    guard_cls.evaluate = lambda self, ctx: []
    broken = probe()
    guard_cls.evaluate = original
    after = probe()
    check("变异｜%s 判据：打坏即变红、恢复即变绿" % category,
          before and not broken and after)

# ============ 8. 活路径接线（真正在跑的两条路）============
REQ = {
    "name": "张三", "position": "后端开发",
    "phone": "138-0000-0000", "email": "zhang@example.com", "github": "",
    "summary": "计算机专业应届生。",
    "education": [{"school": "某大学", "major": "计算机科学与技术",
                   "degree": "本科", "start": "2023", "end": "2027"}],
    "experiences": [{"name": "订单系统", "role": "后端开发",
                     "start": "2025.01", "end": "2025.06",
                     "bullets": ["做后端接口开发", "参与需求评审", "协助排查问题"]}],
    "skills": ["Python", "FastAPI"], "certifications": [],
}
STRUCTURED = {
    "name": "张三", "position": "后端开发",
    "contact": {"phone": "138-0000-0000", "email": "zhang@example.com", "github": ""},
    "summary": "后端开发工程师，持续赋能业务，形成端到端闭环。",
    "education": [{"school": "某大学", "major": "计算机科学与技术",
                   "degree": "本科", "start": "2023", "end": "2027"}],
    "experiences": [{"name": "某科技有限公司", "role": "后端开发",
                     "start": "2025.01", "end": "2025.06",
                     "bullets": ["主导接口重构，响应时间降低 40%",
                                 "引入缓存，支撑 5000 QPS"]}],
    "skill_groups": {}, "skills": ["Python", "FastAPI"], "certifications": [],
}
TEXT = ("某科技有限公司 后端开发\n主导接口重构，响应时间降低 40%\n"
        "引入缓存，支撑 5000 QPS\n持续赋能业务，形成端到端闭环")

# 路径①：UI 生成成功后本地补算（服务端返回的结果里没有 guard）
server_like = {"structured": STRUCTURED, "resume_text": TEXT,
               "ats": {"score": 80}, "review": {"score": 75}}
out = rg.guard_result(REQ, server_like, "招聘后端开发，要求 FastAPI")
check("活路径①｜guard_result 补出 guard 字段", "guard" in out)
check("活路径①｜抓到编造", out["guard"]["stats"]["counts"]["编造"] >= 2)
check("活路径①｜没有破坏服务端原有字段",
      out["ats"] == {"score": 80} and out["review"] == {"score": 75})

# 路径②：改排序 / 换候选后的重算
out2 = rg.revalidate(STRUCTURED, "招聘后端开发，要求 FastAPI",
                     source_text=rg.source_text_of(REQ))
check("活路径②｜revalidate 返回 guard", "guard" in out2)
check("活路径②｜重算后同样抓到编造", out2["guard"]["stats"]["counts"]["编造"] >= 1)

check("活路径｜source_text_of 组装出用户原始输入",
      "某大学" in rg.source_text_of(REQ))

out3 = rg.revalidate(STRUCTURED, "JD")
check("活路径｜没传 source_text → 降级报「无法核对」且不谎报 high",
      any(i["category"] == "编造" and i["severity"] == "low" for i in out3["guard"]["issues"])
      and not any(i["category"] == "编造" and i["severity"] == "high"
                  for i in out3["guard"]["issues"]))

check("活路径｜error 结果原样返回，不塞 guard",
      "guard" not in rg.guard_result(REQ, {"error": "生成失败"}))

# ============ 9. UI 接线（AST 结构断言）============
PAGE = _ROOT / "offerflow_desktop" / "ui" / "pages" / "resume_generate.py"
tree = ast.parse(PAGE.read_text(encoding="utf-8"))


def method(name):
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    return None


def call_attrs(node, attr):
    out_calls = []
    for sub in ast.walk(node) if node else []:
        if isinstance(sub, ast.Call) and isinstance(sub.func, ast.Attribute) \
                and sub.func.attr == attr:
            out_calls.append(sub)
    return out_calls


def assigns(node, attr):
    for sub in ast.walk(node) if node else []:
        if isinstance(sub, ast.Assign):
            for tgt in sub.targets:
                if isinstance(tgt, ast.Attribute) and tgt.attr == attr:
                    return True
    return False


def reachable_calls(fn_name, attr, _seen=None):
    """从某个方法出发，沿**本模块内的方法调用**收集它能触达的 `attr(...)` 调用。

    为什么要按调用链找，而不是直接扫这个方法的函数体：`_refresh_after_edit`
    与 `_on_variants` 两处 `revalidate(...)` 后来被收敛进 `_revalidate_current()`
    （顺带把「整体替换 `_current_result`」改成合并回写，避免丢 `id`/`tips`/
    `fidelity`）。函数体里不再直接出现 `revalidate`，只扫函数体就会误报
    「没传 source_text」—— 真正要守的是「这两条编辑路径最终都会走一次
    **带 source_text** 的守门重算」，调用链判据与实现形状无关。
    """
    _seen = set() if _seen is None else _seen
    if fn_name in _seen:
        return []
    _seen.add(fn_name)
    node = method(fn_name)
    if node is None:
        return []
    found = list(call_attrs(node, attr))
    for sub in ast.walk(node):
        if isinstance(sub, ast.Call) and isinstance(sub.func, ast.Attribute):
            found.extend(reachable_calls(sub.func.attr, attr, _seen))
    return found


def every_call_passes(calls, kwarg):
    """每一处调用都显式传了这个关键字参数（空集不算通过）。"""
    return bool(calls) and all(any(k.arg == kwarg for k in c.keywords) for c in calls)


check("UI｜_generate 缓存了 self._last_req", assigns(method("_generate"), "_last_req"))
check("UI｜_on_generated 调用了 guard_result", bool(call_attrs(method("_on_generated"),
                                                                "guard_result")))
check("UI｜存在 _source_text 方法", method("_source_text") is not None)
check("UI｜_refresh_after_edit 最终走到带 source_text 的 revalidate",
      every_call_passes(reachable_calls("_refresh_after_edit", "revalidate"),
                        "source_text"))
check("UI｜_on_variants 最终走到带 source_text 的 revalidate",
      every_call_passes(reachable_calls("_on_variants", "revalidate"),
                        "source_text"))

# ============ 汇总 ============
print()
if FAILS:
    print("未通过 %d 项：" % len(FAILS))
    for item in FAILS:
        print("  -", item)
    sys.exit(1)
print("生成守门层测试全部通过")
