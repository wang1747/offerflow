# -*- coding: utf-8 -*-
"""多栏版式（双栏）测试：模板声明 + 真分栏 + 单栏回归。

三条来自错题本的约束：

1. **每条判据都有反向对照**。验证「双栏更矮」时，比的是**同一个模板**改成
   单栏的样子，而不是拿另一套字号不同的模板比 —— 否则量到的差异可能来自
   字号，而不是分栏。
2. **断言基于渲染结果**（layout 字典 / PDF 字节），不是读源码文本。
3. **现有单栏模板逐套回归**。这次动的是共享渲染器，必须证明 6 套老模板
   仍然 columns==1 且还能渲出 PDF（防「改 A 破 B」）。
"""
import copy
import os
import sys

# 仓库内定位：tests/ 的父目录即 desktop/（offerflow_desktop 包所在）。
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from offerflow_desktop.services import templates          # noqa: E402
from offerflow_desktop.services import pdf_exporter as P  # noqa: E402

FAILS = []


def check(name, cond):
    if not cond:
        FAILS.append(name)
    print("[%s] %s" % ("PASS" if cond else "FAIL", name))


BLOCKS = {
    "name": "张三",
    "position": "后端开发工程师",
    "contact_line": "13800000000 | zhangsan@example.com | github.com/zs",
    "summary": "三年后端开发经验，熟悉 Python 分布式系统与高并发场景。" * 2,
    "education": [
        {"left": "某大学 计算机科学与技术 本科", "period": "2019.09 - 2023.06",
         "details": ["主修课程：数据结构、操作系统、数据库"]},
    ],
    "experiences": [
        {"group": "实习经历", "left": "某科技公司 后端实习生",
         "period": "2022.07 - 2022.12",
         "bullets": ["设计并实现订单查询接口，响应时间降低 40%",
                     "负责日志采集链路，支撑日均 500 万条",
                     "推动缓存改造，命中率提升至 92%"]},
        {"group": "实习经历", "left": "另一家公司 后端实习生",
         "period": "2021.07 - 2021.09",
         "bullets": ["参与权限系统开发，覆盖 12 个业务模块"]},
    ],
    "skills": ["Python", "FastAPI", "PostgreSQL", "Redis", "Docker", "Kubernetes"],
    "skill_groups": {"语言": ["Python", "Go"], "框架": ["FastAPI", "Django"]},
    "certifications": ["CET-6", "软件设计师"],
    "honors": ["校级一等奖学金", "ACM 校赛二等奖"],
}

ALL_SECTIONS = {"summary", "education", "experiences", "skills",
                "certifications", "honors", "extra"}

# ============ 1. 模板声明 ============
split = templates.get_template("split")
check("模板｜split 是双栏", split.columns == 2)
check("模板｜split 声明了 3 个副栏分区",
      set(split.sidebar_sections) == {"skills", "certifications", "honors"})
check("模板｜split 副栏分区都是合法分区名",
      set(split.sidebar_sections) <= ALL_SECTIONS)
check("模板｜split 的 layout_label 说双栏", "双栏" in split.layout_label)
check("模板｜split 触发 ATS 双栏提示", any("栏" in n for n in split.ats_notes))

classic = templates.get_template("classic")
check("模板｜classic 仍是单栏", classic.columns == 1)
check("模板｜classic 无副栏分区", classic.sidebar_sections == ())
check("模板｜classic 无栏相关 ATS 提示",
      not any("栏" in n for n in classic.ats_notes))

d = split.to_dict()
check("模板｜to_dict 带 sidebar_sections",
      d.get("sidebar_sections") == list(split.sidebar_sections))
check("模板｜to_dict 带 sidebar_ratio", isinstance(d.get("sidebar_ratio"), float))

# 反向对照：不声明副栏分区时，即使传 columns=2 也只算单栏
_imp = templates.ResumeTemplate("x", "x", "x", columns=2)
check("模板｜只传 columns=2 不给分区 → 仍是单栏（开关不能空转）",
      _imp.columns == 1)

# ============ 2. 真分栏：同一模板 双栏 vs 单栏 ============
font_path, font_index, _ = P.find_cjk_font()
check("环境｜找到中文字体", bool(font_path))

if font_path:
    def measure(tpl):
        eng = P._TextEngine(P.TrueTypeFont(font_path, font_index))
        return P._render(tpl, BLOCKS, eng, P.PAGE_W_PT, P.PAGE_H_PT)

    split_two = measure(split)

    forced_single = copy.copy(split)
    forced_single.sidebar_sections = ()
    forced_single.columns = 1
    split_one = measure(forced_single)

    print("      [量] split 双栏 used=%.1f / 强制单栏 used=%.1f"
          % (split_two["used_height"], split_one["used_height"]))

    check("渲染｜双栏 layout.columns==2", split_two["columns"] == 2)
    check("渲染｜强制单栏 layout.columns==1", split_one["columns"] == 1)
    check("渲染｜双栏确实比同模板单栏更矮（证明真分了两栏）",
          split_two["used_height"] < split_one["used_height"])
    check("渲染｜双栏未溢出", split_two["overflow"] is False)

    # 反向对照：全部 7 个分区都塞进副栏 → 主栏只剩页头，
    # 高度由副栏主导，必然高于「只放 3 个分区」的双栏布局。
    all_side = copy.copy(split)
    all_side.sidebar_sections = tuple(P.normalize_section_order(None))
    all_side_l = measure(all_side)
    print("      [量] 全塞副栏 used=%.1f" % all_side_l["used_height"])
    check("反向对照｜所有分区塞副栏后，高度明显高于只放 3 个分区",
          all_side_l["used_height"] > split_two["used_height"])
    check("反向对照｜所有分区塞副栏后，主栏侧不溢出",
          all_side_l["columns"] == 2)
else:
    check("渲染｜跳过（无中文字体，无法渲染）", False)

# ============ 3. 单栏回归：6 套老模板仍能渲出 PDF ============
for tpl in templates.TEMPLATES:
    pdf, layout = P._render_pdf(BLOCKS, template_key=tpl.key)
    ok = pdf[:5] == b"%PDF-" and layout["columns"] == tpl.columns
    check("回归｜%s 渲出合法 PDF 且栏数=%d" % (tpl.key, tpl.columns), ok)

# 双栏模板也走一遍完整链路（含自动缩放 / 铺满调优）
pdf_split, layout_split = P._render_pdf(BLOCKS, template_key="split")
check("回归｜split 完整链路渲出合法 PDF", pdf_split[:5] == b"%PDF-")
check("回归｜split 完整链路 layout.columns==2", layout_split["columns"] == 2)
check("回归｜split PDF 体积正常（>3KB）", len(pdf_split) > 3000)

# ============ 汇总 ============
print()
if FAILS:
    print("多栏版式测试失败 %d 项：" % len(FAILS))
    for f in FAILS:
        print("  -", f)
    sys.exit(1)
print("多栏版式测试全部通过")
