"""正文分区的规范顺序与标签（口径层，不放任何渲染逻辑）。

**为什么单独有这么一个模块**：同一份分区顺序原来在代码里有三份拷贝 ——

    pdf_exporter._render         （PDF：渲染）
    exporter._estimate_height    （Word：估高，给一页约束用）
    exporter._render_blocks      （Word：渲染）

三份拷贝在「顺序固定」的年代只是重复；一旦顺序变成用户可配，它们会立刻
分叉，而且分叉的后果很难看：Word 的估高按 A 顺序算、渲染按 B 顺序算 ⇒
「估出来一页、印出来两页」；PDF 与 Word 同一份数据小节顺序还不一样。
所以顺序只允许在这一个文件里定义，渲染两边都来取。

放在 core 而不是 services：这里是**数据口径**（分区叫什么、按什么顺序排），
不是版式参数。core 层不依赖 services，services 与 UI 都依赖 core。
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional

# 页头（姓名 / 求职意向 / 联系方式）之外的全部正文分区。
# 这个顺序同时是：默认渲染顺序 + 用户在 UI 里排序的初始值 + 非法输入的回退。
BODY_SECTIONS: List[str] = [
    "summary",
    "education",
    "experiences",
    "skills",
    "certifications",
    "honors",
    "extra",
]

# 显示名。渲染时的标题文案也从这里取，避免「UI 里叫技能、导出稿里叫专业技能」。
SECTION_LABELS: Dict[str, str] = {
    "summary": "个人总结",
    "education": "教育经历",
    "experiences": "实习/工作经历",
    "skills": "技能",
    "certifications": "证书",
    "honors": "荣誉奖项",
    "extra": "其他",
}

# ATS 语境下的推荐顺序：把「技能」提到「经历」之前。
# 依据是 ATS 通常先按小节标题切块再做关键词匹配，技能类关键词集中成块
# 比散落在各段经历里更容易被整块命中。只在用户的实际顺序与它不同时才提示，
# 不然就是一句永远刷屏的废话。
RECOMMENDED_SECTION_ORDER: List[str] = [
    "education",
    "skills",
    "experiences",
    "summary",
    "certifications",
    "honors",
    "extra",
]


def normalize_section_order(order: Optional[Iterable[Any]]) -> List[str]:
    """把外部给的分区顺序规整成「合法、不重、不漏」的一份排列。

    规则（顺序敏感，缺一不可）：
    1. 不是 BODY_SECTIONS 里的名字 → **丢弃**。UI 传脏数据、旧版本存下来的
       已删分区、或者有人手工改了配置文件，都不该让渲染循环拿到未知 key。
    2. 重复的名字只保留第一次出现的位置。
    3. 没被提到的分区，按 BODY_SECTIONS 的默认相对顺序补在末尾。

    返回值的长度恒等于 ``len(BODY_SECTIONS)``，且是 BODY_SECTIONS 的一个排列。
    这条不变量是渲染层的安全保证：无论输入多脏，渲染循环都只遍历已知分区一次。
    """
    if not order:
        return list(BODY_SECTIONS)
    if isinstance(order, (str, bytes)):
        # 传进来一个字符串是最容易犯的调用错误（"summary" 会被当成可迭代字符，
        # 逐字丢进 seen 里全被判非法，最后静默回退默认序——很难查）。
        # 这里直接当作「单个分区名」处理，语义比静默回退清楚。
        order = [order]
    try:
        candidates = list(order)
    except TypeError:
        # 根本不是可迭代对象（配置文件里被手改成数字、布尔…）。
        # **这里绝不能抛**：这个值来自用户本机的 system_config.json，
        # 一个写错的数字就能让「导出」按钮点不动，代价远大于"顺序没生效"。
        return list(BODY_SECTIONS)
    seen: List[str] = []
    for name in candidates:
        key = str(name or "").strip()
        if key in SECTION_LABELS and key not in seen:
            seen.append(key)
    seen.extend(name for name in BODY_SECTIONS if name not in seen)
    return seen


def section_labels_in_order(order: Optional[Iterable[Any]]) -> List[str]:
    """按顺序给出显示名，供建议文案与 UI 使用。"""
    return [SECTION_LABELS[name] for name in normalize_section_order(order)]


def order_equals_default(order: Optional[Iterable[Any]]) -> bool:
    """规整后的顺序是否就是默认顺序。"""
    return normalize_section_order(order) == list(BODY_SECTIONS)


def is_ats_recommended(order: Optional[Iterable[Any]]) -> bool:
    """规整后的顺序是否已经等于 ATS 推荐顺序。"""
    return normalize_section_order(order) == list(RECOMMENDED_SECTION_ORDER)
