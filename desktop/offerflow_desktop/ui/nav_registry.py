# -*- coding: utf-8 -*-
"""侧栏导航注册表 —— 「有哪些页面」的唯一事实来源（对齐网页版 modules.tsx）。

## 为什么需要它

改造前导航项写在 ``main_window.py`` 的一个字面量列表里，用
``("__group__", "工作区", None)`` 这种**位置约定**表达分组，于是：

  * 新增一页要同时改列表、改图标名映射、改测试里的硬编码条数（``== 16``）
  * 「这一页是干什么的」全靠名字猜 ——「简历生成」和「简历优化」光看名字分不清
  * 分组标题混在列表项里，滚动时跟着条目一起跑，视觉上压不住层级
  * 谁可见（管理后台）只能写成运行期 if，散在构造函数里

现在把这些集中到这里；``main_window`` 只负责渲染，测试只对着注册表断言。

## 信息架构（本次重排）

  * **求职 Agent 提到第 2 位**：它是这个产品的主线能力，原先排在工作区第 8 位
    （倒数第一），一个用户最该先看到的入口被埋在列表最底部。
  * **分组按使用场景收口**为 工作区 → 社区 → 系统（→ 管理，仅管理员）。
    不再为「交流中心」这种单项单独开组之外的碎组。
  * **每条都有副标题**：一句话说清「这一页替我做什么」，用于区分相近模块。
    这是网页版侧栏已有的能力，桌面端此前完全缺失。

## 与网页版的差异（有意保留）

桌面端比网页版多两页（模型设置、本地知识库），且**管理员仍能看到求职功能**
而不是像网页版那样只给管理模块 —— 桌面版是单机工作台，管理员也要用求职 Agent。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Tuple, Type

from .pages.admin import AdminPage
from .pages.agent import AgentPage
from .pages.community import CommunityPage
from .pages.cost import CostPage
from .pages.dashboard import DashboardPage
from .pages.interview import InterviewPage
from .pages.job_match import JobMatchPage
from .pages.local_kb import LocalKnowledgeBasePage
from .pages.model_settings import ModelSettingsPage
from .pages.resume_generate import ResumeGeneratePage
from .pages.resume_optimize import ResumeOptimizePage
from .pages.salary import SalaryPage
from .pages.system_settings import SystemSettingsPage
from .pages.tracker import TrackerPage


@dataclass(frozen=True)
class NavItem:
    """一个可点击的导航条目。

    ``key`` 同时是页面栈的键（``MainWindow.pages`` 的键、``navigate()`` 的参数），
    因此**改名等于改契约**，不要随手改。
    """

    key: str
    label: str
    subtitle: str
    icon: str
    page: Type
    admin_only: bool = False
    # 首页标记：窗口起来时默认落在这一页（也用于恢复「上次访问的页面」）
    is_home: bool = False


@dataclass(frozen=True)
class NavGroup:
    key: str
    label: str
    items: Tuple[NavItem, ...]


NAV_GROUPS: Tuple[NavGroup, ...] = (
    NavGroup(
        key="work",
        label="工作区",
        items=(
            NavItem("首页", "首页", "求职进展一览", "home", DashboardPage, is_home=True),
            # 主线能力，放第 2 位（原先在工作区倒数第一）
            NavItem("求职 Agent", "求职 Agent", "读 JD 给行动建议", "sparkle", AgentPage),
            NavItem("简历生成", "简历生成", "没有简历，从零做一份", "resume", ResumeGeneratePage),
            NavItem("岗位分析", "岗位分析", "拆解 JD + 算匹配度", "search", JobMatchPage),
            NavItem("简历优化", "简历优化", "已有简历，针对岗位改", "edit", ResumeOptimizePage),
            NavItem("面试准备", "面试准备", "面经库 + 模拟面试", "chat", InterviewPage),
            NavItem("薪资谈判", "薪资谈判", "估市场价 + 演练谈薪", "banknote", SalaryPage),
            NavItem("投递追踪", "投递追踪", "投递进度看板", "clipboard", TrackerPage),
        ),
    ),
    NavGroup(
        key="community",
        label="社区",
        items=(
            NavItem("交流中心", "交流中心", "分享经验与建议", "community", CommunityPage),
        ),
    ),
    NavGroup(
        key="system",
        label="系统",
        items=(
            NavItem("AI 用量与成本", "AI 用量与成本", "查看消耗明细", "cost", CostPage),
            NavItem("模型设置", "模型设置", "平台模型 / 自有 Key", "model", ModelSettingsPage),
            NavItem("本地知识库", "本地知识库", "离线语料与索引", "kb", LocalKnowledgeBasePage),
            NavItem("系统设置", "系统设置", "账号、主题与数据", "settings", SystemSettingsPage),
        ),
    ),
    NavGroup(
        key="admin",
        label="管理",
        items=(
            NavItem("管理后台", "管理后台", "用户、用量与健康", "admin", AdminPage,
                    admin_only=True),
        ),
    ),
)


def iter_items(*, is_admin: Optional[bool]) -> Tuple[NavItem, ...]:
    """按角色摊平出可见条目。

    ``is_admin`` 为 ``None`` 表示「角色还没查出来」（老版本登录缓存），
    此时**不显示**管理入口 —— 宁可晚几秒出现，也不要在非管理员面前闪一下
    再消失（原先的 ``_maybe_add_admin_page`` 就是这个语义）。
    """
    out = []
    for group in NAV_GROUPS:
        for item in group.items:
            if item.admin_only and not is_admin:
                continue
            out.append(item)
    return tuple(out)


def all_items() -> Tuple[NavItem, ...]:
    """全部条目（含管理），供测试与文档使用。"""
    return tuple(i for g in NAV_GROUPS for i in g.items)


def item_by_key(key: str) -> Optional[NavItem]:
    for item in all_items():
        if item.key == key:
            return item
    return None


def home_item() -> Optional[NavItem]:
    """默认落地页（注册表里标了 ``is_home`` 的那条）。"""
    for item in all_items():
        if item.is_home:
            return item
    items = all_items()
    return items[0] if items else None


def groups_for(*, is_admin: Optional[bool]) -> Tuple[NavGroup, ...]:
    """过滤掉「过滤后一个可见条目都没有」的分组。

    必要性：非管理员时「管理」组会空掉。若不过滤，侧栏会留下一个
    光秃秃的「管理」标题，看起来像加载失败。
    """
    out = []
    for group in NAV_GROUPS:
        visible = tuple(
            i for i in group.items if not (i.admin_only and not is_admin)
        )
        if visible:
            out.append(NavGroup(group.key, group.label, visible))
    return tuple(out)
