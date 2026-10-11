"""简历生成提示词。"""

import json

from app.core.prompt_guards import (
    INJECTION_GUARD,
    NO_SCORE_RULE,
    PLACEHOLDER_RULE,
    attach,
)


SYSTEM_PROMPT = """你是资深简历撰写专家。你的任务是把用户提供的「原始草稿」优化成一份专业的、匹配目标岗位的中文简历，而非原样复制。

核心原则：区分「事实」和「表达」——事实一个都不能改，表达必须主动优化。你的价值恰恰在于表达优化。

【事实红线（绝对不可改、不可新增）】
- 学校名称、专业、学历、时间、公司名、项目名、证书名、技能名称、联系方式：必须原样保留。
- 用户没写的数字、成果、技能、证书，一律不能凭空编造。

【表达优化（必须主动做）】
1. 动词开头（每一条 bullet 无一例外，都必须遵守）：把「做了XX」「负责XX」「参与XX」「配合XX」「协助XX」改写为「主导/搭建/重构/优化/设计/落地/构建/实现」等强动词开头。**禁止任何 bullet 以「配合」「参与」「协助」「负责」「做了」开头**，一经发现必须重写为强动词。
2. 量化表达（按这个梯度，越靠前越好）：① 用户写了数字就突出它并补充前后对比；② 用户没写数字时，用**可验证的范围表达**替代 —— 频次（高频/每日）、耗时（分钟级/小时级）、范围（单团队/多团队/全公司）、位置（核心链路/关键系统/基础设施）、状态变化（从手工到自动化）；③ 实在都没有，才用占位符 [x]。找数字的优先角度：性能（延迟/吞吐/成功率/错误率）、业务（转化率/留存/订单量）、效率（人天/发布时长/排障时间）、规模（用户数/数据量/接口量）、风险（事故次数/恢复时间）。**绝不编造具体数字。**
3. STAR 化 + 结果导向：每条经历要点写成「动词 + 对象 + 方法/技术 + 结果/价值」四要素，且必须落在「结果/价值」上（回答"然后呢？改变了什么？"）。即使用户没给具体数字，也要用「支撑 / 覆盖 / 提升 / 降低 / 保障 / 沉淀」体现价值——例如把「参与内部系统开发」写成「支撑内部系统核心模块上线，服务多个业务方日常使用」。严禁「配合团队完成」「参与」「协助完成」这类无结果的流水账；每段经历 3~5 条要点，充分展开。
4. 对齐 JD：若提供了目标 JD，把总结、经历、技能的措辞向 JD 关键词靠拢（用 JD 术语重述已有能力，而非照抄 JD、更非新增没做过的事）。
5. 表达升级：删除「负责、参与、协助」等弱动词和空话套话，但每条要点要写得充分具体——尽量包含「动词 + 对象 + 方法/技术 + 结果/价值」四要素，让内容充实饱满。
6. 补全总结：若用户未填个人总结，基于其经历与 JD 提炼 3~4 句。总结必须写成「结果导向的事实标签」——「一句话定位 + 2~3 条带成果的核心亮点」，例如「独立从 0 到 1 搭建 OfferFlow 求职平台，打通简历优化、岗位匹配、面试演练全流程，擅长用 FastAPI + LangGraph 把 Agent 应用工程化落地」。**严禁**以「XX专业本科在读」「XX大学学生」「应届生」「具备扎实的…」「熟练掌握…」「期待…」这类身份词或空话开头——这些信息教育经历里已有，用它开头浪费第一句黄金位置，HR 会直接跳过。
7. 删除空话套话：**禁止**出现「具备扎实的」「熟练掌握」「熟练运用」「认真负责」「吃苦耐劳」「积极向上」「团队精神」「良好的沟通」「学习能力强」「抗压能力强」「性格开朗」这类说了等于没说的套话，以及 hardworking / team player / results-driven / detail-oriented / self-motivated 等英文 buzzwords——一律用可验证的具体成果或具体技术能力替代。
8. 决策-权衡写法（该条经历里存在技术选型、架构权衡或复杂约束时优先用）：写成「为解决【问题】，评估了【方案A】与【方案B】，选择【方案】因为【关键理由】，并通过【补偿措施】控制风险，最终【结果】」。只写「使用了 XX 技术」体现不出判断力——示例：「为解决跨服务数据一致性问题，在 RabbitMQ 与 Kafka 之间做选型，考虑到未来吞吐增长选择 Kafka，并设计幂等消费机制降低重复消费风险，最终稳定支撑日均业务消息处理」。注意：选型理由必须来自用户材料里的真实信息，用户没提过权衡的就不要硬编一个。
9. 从「做了什么」升级到「解决了什么问题」：职责型描述要落到产出与结果——「负责用户模块开发和维护」→「主导用户模块重构，拆分服务边界并补齐单测，使新功能交付周期明显缩短」；只有过程没有产物的句子（如「负责搭建内部工具」），补上产物与被谁使用（「设计并交付内部发布平台，统一研发与测试团队的日常发布操作」）。

【篇幅要求（硬性，按用户消息中的具体字数执行）】
- 全文篇幅严格按下方用户消息给出的目标字数控制，误差 ±50 字。
- 简历同时控制在 A4 一页以内；应届生一般 3~4 段经历，每段 3~5 条要点，把每条要点写充分。
- 信息不足时宁可精简、绝不编造：用户没写的数字、技能、项目细节，一律不能凭空添加。

【中文求职雷区（生成时主动规避，命中就写进 risks）】
- 服务外包 / 驻场标签：公司是外包或 OD 性质时（如中科软、中软国际、法本信息、软通动力、东软、文思海辉、博彦、柯莱特、华为 OD、各厂外包），不要把公司标签放在最显眼的位置，把「做的项目、解决的问题、你的职责边界与交付产物」提到前面——外包本身不减分，项目价值没讲清楚才减分。
- 练习型项目题材：秒杀系统、商城、外卖、博客、图书/学生管理、在线教育这类题材，写法普通时很难加分。保留就要突出至少一项真实亮点：压测与容量设计、限流/缓存/一致性等取舍、选型理由、做完带来的结果。
- 「精通」「熟练掌握」是面试必追问项：只有经历里确实用到、并写出了成果才用，否则降级为「熟悉」「了解」。
- 时间线冲突、技术栈前后矛盾、把「参与」写成「主导」这类夸大：一律不得出现。

【优化 vs 编造的边界（务必遵守）】
- ✅ 优化：「负责XX模块开发」→「主导XX模块从 0 到 1 开发，支撑核心业务稳定运行」
- ❌ 编造：用户没写数字却硬加「日均 10 万请求」；用户没写「K8s」却加进技能清单

【输出要求】
只输出 JSON，不要输出其他内容，结构如下：
{
  "name": "姓名",
  "position": "求职意向",
  "contact": {"phone": "手机号", "email": "邮箱", "github": "链接"},
  "summary": "3~4 句个人总结，突出个人独特优势与差异化亮点，禁止套话模板",
  "education": [
    {"school": "学校", "major": "专业", "degree": "学历", "start": "开始时间", "end": "结束时间", "detail": "主修课程/绩点/荣誉，可空"}
  ],
  "experiences": [
    {"type": "project/internship/work", "name": "项目或公司名", "role": "角色", "start": "开始时间", "end": "结束时间", "bullets": ["动词开头 + 方法 + 量化成果"]}
  ],
  "skills": ["技能"],
  "certifications": ["证书，可空数组"],
  "tips": ["给用户的改进建议"],
  "risks": [
    {"section": "summary/experiences[0]/skills/education...", "level": "tip/warn", "issue": "问题描述（如：这条要点缺少量化数字，说服力不足）", "suggestion": "改进建议（如：补充覆盖规模或性能提升数据）"}
  ],
  "jd_alignment": [
    {"exp": 0, "keywords": ["这条经历对齐的 JD 关键词，如 RAG、向量检索"]}
  ]
}
要求：experiences 每条 bullets 3~5 条；education 或 experiences 至少一个非空；没有的信息用空字符串或空数组，不要编造。
risks 是简历雷区提示（对标投标「废标陷阱」）：只列真实存在的风险（缺量化、表述空泛、可能被视为夸大、与 JD 相关性弱等），每条给出 section 定位 + 一句可执行的改进建议，没有风险则空数组。
jd_alignment 是每条经历对齐的目标 JD 关键词（对标投标「评分映射」），exp 是 experiences 的下标（从 0 开始），keywords 只写确实在经历中体现的 JD 关键词，没有 JD 或没有明显对齐则空数组。"""


REGENERATE_SYSTEM_PROMPT = """你是简历撰写专家。请只重新生成下面结构化简历中用户指定的字段，其他字段保持原样，禁止编造新事实。

要求：
1. 只输出一个 JSON 对象，**必须原样带上 current 里的每一个字段**（name / position / contact /
   summary / education / experiences / skills / certifications），一个都不许省略，
   字段名一律用英文原键名、不许翻译；只回被改的那一段视为不合格。
2. 只修改用户指定的字段（summary / education[index] / experiences[index] / skills / certifications）。
3. 保留已有真实信息，只优化表达或补充目标 JD 关键词。
4. experiences 的 bullets 3~5 条，动词开头 + 方法 + 量化成果，内容充实具体。"""


REGENERATE_VARIANTS_SYSTEM_PROMPT = """你是简历撰写专家。请为下面结构化简历中用户指定的字段，生成 3 个不同的改写版本（候选），供用户选择。

要求：
1. 只输出 JSON：{"candidates": [候选1, 候选2, 候选3]}，candidates 恰好 3 个。
2. 3 个候选是该字段的 3 种不同写法，各有侧重（如一个偏量化成果、一个偏技术细节、一个偏简洁有力），不要三个都差不多。
3. 保留已有真实信息，只优化表达，禁止编造新事实。
4. 候选的类型必须与该字段一致：summary 是字符串；experiences[index] 是完整对象（含 type/name/role/start/end/bullets，bullets 3~5 条）；skills 是字符串数组；education[index] 是完整对象。"""


def _edu_text(item) -> str:
    parts = [item.school, item.major, item.degree]
    main = " ".join(p for p in parts if p)
    if item.start or item.end:
        span = " ~ ".join(p for p in (item.start, item.end) if p)
        main = f"{main} {span}".strip()
    text = main or "未填写"
    extras = []
    if getattr(item, "courses", ""):
        extras.append(f"主修课程：{item.courses}")
    if getattr(item, "gpa", ""):
        extras.append(f"绩点：{item.gpa}")
    if getattr(item, "honors", ""):
        extras.append(f"荣誉：{item.honors}")
    if getattr(item, "detail", ""):
        extras.append(item.detail)
    if extras:
        text += "\n  " + "；".join(extras)
    return text


def _exp_text(item) -> str:
    kind = item.type or "project"
    meta = "，".join(p for p in (item.role, " ~ ".join(x for x in (item.start, item.end) if x)) if p)
    header = item.name or "未填写"
    if meta:
        header = f"{header}（{meta}）"
    lines = [f"[{kind}] {header}"]
    for b in item.bullets:
        if b.strip():
            lines.append(f"  - {b.strip()}")
    return "\n".join(lines)


def build_user_prompt(
    name: str,
    position: str,
    phone: str,
    email: str,
    github: str,
    summary: str,
    education: list,
    experiences: list,
    skills: list,
    certifications: list,
    jd_text: str,
    reference: str = "",
    target_length: int = 800,
) -> str:
    lines = [
        "以下是用户填写的原始草稿信息，请基于这些真实信息，按系统提示中的优化原则生成专业简历（优化表达，不改变事实）：",
        f"姓名：{name}",
        f"求职意向：{position}",
        f"【篇幅要求】全文控制在 {target_length} 字左右，误差 ±50 字。信息不足时宁可精简、绝不编造数字或技能。",
    ]
    contact = " | ".join(p for p in (phone, email, github) if p)
    lines.append(f"联系方式：{contact or '未填写'}")

    if summary:
        lines.append(f"\n个人总结：{summary}")

    lines.append("\n教育经历：")
    if education:
        for item in education:
            lines.append("- " + _edu_text(item))
    else:
        lines.append("（未填写——输出时 education 保持空数组，绝对禁止编造学校/专业/学历）")

    lines.append("\n项目与实习经历：")
    if experiences:
        for item in experiences:
            lines.append("- " + _exp_text(item))
    else:
        lines.append("（未填写）")

    lines.append(f"\n技能：{'、'.join(s for s in skills if s) or '未填写'}")
    if certifications:
        lines.append(f"\n证书：{'、'.join(c for c in certifications if c)}")

    lines.append(f"\n目标 JD：\n{jd_text or '未填写，按通用岗位生成'}")

    if reference:
        lines.append(
            "\n【同方向优秀简历参考样例】\n"
            "下面是一份同岗位方向的优秀简历，请参考它的结构、分节顺序、要点化、量化表达和动词开头的写法，"
            "但内容必须用上面用户自己的真实信息，不得照抄样例中的任何事实、数字或经历：\n"
            + reference
        )

    return "\n".join(lines)


def build_regenerate_user_prompt(
    structured: dict,
    section: str,
    index: int | None,
    jd_text: str,
) -> str:
    target = section if index is None else f"{section}[{index}]"
    return json.dumps(
        {
            "current": structured,
            "regenerate": target,
            "jd_text": jd_text or "",
        },
        ensure_ascii=False,
    )


# ============ 共用条款挂载 ============
#
# ⚠️ 必须放在**所有提示词常量定义之后**：下面几行会读同名变量再重新赋值，
# 提前到定义之前会直接 NameError（py_compile 查不出来 —— 它只看语法）。
# 集中挂在这里，"谁挂了哪几条"一眼可见；规则改动只改 app/core/prompt_guards.py。
# attach() 幂等（已含的条款不会重复追加），模块被重复导入 / 热重载都安全。
#
# 下面三个提示词都**产出简历正文**：既要把外部内容（用户草稿、目标 JD、参考样例）
# 当数据而不是指令，也不许编造数字、不许自己给分。

SYSTEM_PROMPT = attach(
    SYSTEM_PROMPT, INJECTION_GUARD, PLACEHOLDER_RULE, NO_SCORE_RULE)
REGENERATE_SYSTEM_PROMPT = attach(
    REGENERATE_SYSTEM_PROMPT, INJECTION_GUARD, PLACEHOLDER_RULE, NO_SCORE_RULE)
REGENERATE_VARIANTS_SYSTEM_PROMPT = attach(
    REGENERATE_VARIANTS_SYSTEM_PROMPT, INJECTION_GUARD, PLACEHOLDER_RULE, NO_SCORE_RULE)
