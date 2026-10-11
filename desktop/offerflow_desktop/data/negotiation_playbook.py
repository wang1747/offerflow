# -*- coding: utf-8 -*-
"""内置薪资谈判话术库（随包分发，离线可用）。

来源：Salary-Negotiation-Skill
      https://github.com/Ssupercoder/Salary-Negotiation-Skill
      strategies/negotiation_strategies.py —— 4 个核心策略 + 5 个高压场景话术卡。
加工：保留「策略 / 场景 → 错误回法 / 推荐回法 / 风险 / 后续跟进」的结构，
      并把原库里的岗位专属例子（视觉设计师等）**泛化为通用话术**。
用途：`services.salary_negotiation.coach_answer` 把它作为**打法对照**注入教练提示词，
      让点评能指着具体策略说「这时候该用『替代补偿』」，而不是泛泛而谈。
"""

from typing import Any, Dict, List

#: 4 个核心谈判策略（贯穿整个谈判过程）。
NEGOTIATION_STRATEGIES: List[Dict[str, str]] = [
    {
        "key": "counter_inquiry",
        "name": "反问询价",
        "idea": "不先报价，让 HR 先暴露预算区间",
        "sample": "在谈具体数字之前，我想先了解一下这个岗位的薪酬带宽，以及贵司对这个级别的定位。"
                  "基于我目前的背景和手上其他机会，我相信我们能找到一个双方都满意的数字。",
        "tip": "如果 HR 坚持要你先说，就**给区间不给点**："
               "「我了解市场上这个级别的价位在 A~B 之间，基于我的经验我期望在偏上的位置。」",
    },
    {
        "key": "star_value_statement",
        "name": "STAR 价值陈述",
        "idea": "用 STAR 证明「我值这个价」，而不是说「我需要这个钱」",
        "sample": "在上一份经历里，我负责的 XX 项目（S），目标是 XX（T）。"
                  "我主导了 XX、协调了 N 人（A），最终提前交付、把 XX 指标提升了 N%（R）。",
        "tip": "结果必须量化（百分比 / 金额 / 时间）；强调与目标岗位的契合度和稀缺技能。",
    },
    {
        "key": "alternative_compensation",
        "name": "替代补偿",
        "idea": "base 谈不动时，转向其它可谈项",
        "sample": "理解贵司的预算管理。如果 base 确实到了上限，能否在签字费或股票归属速度上做一些调整？"
                  "这些对我来说也有实质价值，我更倾向贵司的平台，所以希望能找到平衡点。",
        "tip": "可谈项：签字费（通常 1~6 个月 base）、股票数量与**归属速度**、绩效奖金保底、"
               "入职后提前评晋升、搬家费/培训预算/设备补贴。所有承诺要**书面确认**。",
    },
    {
        "key": "offer_anchor",
        "name": "Offer 锚定",
        "idea": "用真实存在的其它 offer 做锚点",
        "sample": "我目前手上有另一个机会，总包比贵司高出一些。但我更看好贵司的业务方向，"
                  "如果总包能接近那个水平，我会优先选择贵司。",
        "tip": "三原则：**真实**（不虚构）、**适度**（提及但不炫耀）、**具体**（给数字）。"
               "禁忌：不威胁「不给就去别家」、不透露全部细节、不过度承诺「给到就必来」。",
    },
]

#: 5 个高压场景（HR 常见话术 + 标准应对）。
NEGOTIATION_PRESSURE_SCENARIOS: List[Dict[str, Any]] = [
    {
        "key": "budget_limit",
        "name": "预算有限",
        "hr_line": "这个 offer 已经是这个级别的最高档了，我们确实没有更多预算",
        "wrong": "那好吧，我接受。（直接把谈判空间全让掉）",
        "right": "理解贵司的预算管理。想确认一下，这个上限是这个级别的统一标准，还是针对我这个 offer 的？"
                 "如果 base 确实到了上限，能否在签字费或股票归属速度上做些调整？这些对我来说也有实质价值。",
        "risk": "中",
        "follow_up": ["HR 说是统一标准 → 转向替代补偿", "HR 说确实没空间 → 判断底线是否可接受"],
    },
    {
        "key": "anchor_trap",
        "name": "锚定陷阱",
        "hr_line": "你手上其他 offer 大概什么水平？能透露一下吗？",
        "wrong": "我期望 80k。（被问就把自己的底线直接说出去）",
        "right": "在谈具体数字之前，我想先了解一下这个岗位的薪酬带宽和这个级别的定位。"
                 "基于我目前的背景和手上其他机会，我相信我们能找到一个双方都满意的数字。",
        "risk": "低",
        "follow_up": ["HR 坚持要你先说 → 给区间不给点", "HR 给出预算区间 → 在区间偏上锚定"],
    },
    {
        "key": "pressure_lowball",
        "name": "压价试探",
        "hr_line": "你的期望超出我们的预算了，能不能再低点？比如 XX？",
        "wrong": "那降一点可以吗？（直接自己往下砍）",
        "right": "感谢反馈。我想确认一下，这个数字是基于我这个具体背景的评估，还是这个级别的统一标准？"
                 "另外我想重申一下我带来的价值：XX 项目让 XX 指标提升了 N%。基于这些可量化的成果，我相信我的期望是合理的。",
        "risk": "高",
        "follow_up": ["HR 坚持压价 → 用 STAR 重申价值", "HR 松口 → 小幅确认，别立刻答应"],
    },
    {
        "key": "time_pressure",
        "name": "时间压力",
        "hr_line": "这个 offer 本周内必须确认，过期就作废了",
        "wrong": "那我马上确认。（被 deadline 逼得仓促决定）",
        "right": "理解贵司的招聘流程有时间要求。能否确认一下，这个 deadline 是硬性要求还是有弹性空间？"
                 "我需要认真评估这个机会。如果确实时间紧张，我可以在明天下班前给您明确答复。",
        "risk": "中",
        "follow_up": ["HR 说 deadline 是硬性的 → 争取部分让步并用书面回复", "HR 同意延期 → 用这段时间补齐其它 offer"],
    },
    {
        "key": "level_downplay",
        "name": "职级压低",
        "hr_line": "按我们的职级体系只能给你定到这个级别，虽然你经验很丰富",
        "wrong": "好吧，定这个级别就这个级别。（接受低职级）",
        "right": "理解贵司的职级体系。想确认一下，这个定级是基于我目前背景的评估，还是统一标准？"
                 "我在上一份经历里已经负责 XX、带过 N 人团队，达到的是更高标准。"
                 "如果定级确实无法调整，能否在薪资上反映这部分价值差异？",
        "risk": "高",
        "follow_up": ["HR 坚持定级 → 转向薪资/签字费补偿", "HR 同意重新评估 → 补充材料推进"],
    },
]


def build_coach_playbook(max_chars: int = 1100) -> str:
    """把策略库 + 场景卡压成一段**可注入教练提示词**的打法对照文本。

    教练点评是逐轮的，注入太长会挤占上下文，所以这里只给：
    4 条策略的「一句话要点」+ 5 个场景的「一句话标准应对」。
    """
    lines: List[str] = ["【打法对照（供点评时引用）】"]

    lines.append("核心策略：")
    for s in NEGOTIATION_STRATEGIES:
        lines.append(f"- {s['name']}：{s['idea']}")

    lines.append("常见场景与标准应对：")
    for sc in NEGOTIATION_PRESSURE_SCENARIOS:
        lines.append(f"- {sc['name']}（HR：{sc['hr_line'][:24]}…）→ 应对要点：{sc['right'][:56]}…")

    text = "\n".join(lines)
    if len(text) > max_chars:
        text = text[:max_chars]
    return text
