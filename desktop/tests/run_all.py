"""一键跑全部测试（发版前必跑）。

用法：
    .venv/Scripts/python.exe tests/run_all.py

覆盖：
    静态审计      —— 用了没导入的名字（只会在运行时炸的 NameError）
    别名属性审计  —— 从 A 导入起别名、却调用 A 里不存在的函数（点按钮才崩）
    构造期异步    —— 在 __init__ 里发请求（页面提前销毁会令进程静默崩溃）
    全页面冒烟    —— 逐个构造页面 + 页面覆盖自检
    主窗口重建    —— 反复构造/销毁主窗口（模拟「退出登录后再登录」）
    启动链路冒烟  —— 复刻 main.py 的窗口引导（构造→setup_tray→单实例→show）
                     ＋校验 main.py 调用的每个 window 成员都存在
                     （构造测试看不见的"构造之后才初始化"那一段）
    侧栏注册      —— 页面注册、角色门控、图标渲染、navigate
    对比度/回归   —— 配色可读性与简历复评回归
    高级功能      —— 模板 PDF 预览、模块排序、语义匹配、表达库、连续性
    本地只读副本  —— 三态可区分、离线读副本、写操作不再静默失败、账号隔离
    版式/外观/序  —— 三处渲染取同一份分区顺序、外观与版式解耦、
                     ATS 标注现算、两个导出页真的把设置传下去
    Agent 接线     —— 构造期不启动线程（P0）/ JD·简历·投递三页接线 / 重建不崩
    更新校验      —— 自动更新安装包 SHA256 完整性校验（P1-4）
    增量更新      —— 内容寻址 manifest：只下载变化文件 + 篡改块拒绝 + 引导脚本
    登录态安全    —— Token 经 DPAPI 加密落盘、明文格式兼容（P2-5）
    导出回读自检  —— 把刚导出的 PDF 读回来对内容（含「环境故障 ≠ 内容缺陷」边界）
    撤销/草稿     —— 撤销栈语义、草稿往返、表单快照能贴回且结构不符时拒绝
    模板筛选/ATS  —— 筛选不被外观影响、ATS 视角回读缓存字节、分区顺序拖放
    跟随系统主题  —— resolve_theme 永不返回 "system"，QSS 与图标同源
    日历/ICS 导出 —— RFC5545 结构、字节级 CRLF 与折叠、跨实现回读（装了
                     icalendar 时自动生效）、投递追踪日历视图与导出的 UI 行为
    JSON Resume   —— 与官方 v1.0.0 schema 双向映射；meta.offerflow 无损往返；
                     容错降级（缺 endDate 不擅自补「至今」）；官方 schema 校验
                     （装了 jsonschema 时自动生效）；导出走字节写入
    JSON Resume UI—— .json 导入改道 / 导出按钮接线 / 反馈文案按格式分开
    生成守门层    —— 编造硬事实（数字/年份/机构名 vs 用户原始输入）/ 中文求职
                     风险（外包标签、玩具项目、「精通」无证据）/ AI 腔 / 量化降级
                     建议；四条判据都有反向对照与变异测试；接线断言走 AST；
                     并验证守门层挂在**真实执行路径**上（云端生成后本地补算 +
                     revalidate 重算），而不是挂在无调用方的死函数里
    多栏版式      —— 双栏模板声明（sidebar_sections 而非 columns 开关）、真分栏渲染
                     比同模板单栏更矮、副栏塞满时的反向对照、6 套单栏模板逐套回归
    复用闭环      —— 生成/优化的成果按 source 进「成果池」（下一轮同类任务可召回，
                     这就是越用越好用的机制），导入/上传的材料仍留在材料池且被
                     排除（防自引用，实测自有简历与 query 同源 score=1.000）；
                     质量闸门挡明确不合格的成果（降级回材料池而非丢弃）；
                     回声防护丢弃与当前输入几乎同分的旧版块（防模型照抄）；
                     检索走来源白名单、两路各自配额、随结果落召回轨迹；
                     save_generated / save_optimized 后自动入索引（不靠手动保存）；
                     rebuild 时成果 id 不让给快照（否则被扔回材料池）

退出码 0 = 全绿；非 0 = 有失败（发版流程必须拦住）。
⚠️ 清单里登记了却找不到文件的套件**按失败处理**（不再静默跳过），且末尾按
   **实际执行数**报数 —— 否则「登记名与实际不符」会伪装成全绿。
"""

import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
TESTS = [
    ("静态审计", "test_static_audit.py"),
    ("别名属性审计", "test_alias_attr_audit.py"),
    ("构造期异步审计", "test_ctor_async_audit.py"),
    ("全页面冒烟", "test_pages_smoke.py"),
    ("主窗口重建稳定性", "test_window_rebuild.py"),
    ("启动链路冒烟（含托盘/单实例）", "test_window_startup.py"),
    ("侧栏注册与角色门控", "test_nav_registration.py"),
    ("配色对比度", "test_contrast_metric.py"),
    ("简历复评回归", "test_resume_review_regression.py"),
    ("高级功能可用性", "test_advanced_features.py"),
    ("本地只读副本", "test_local_cache.py"),
    ("本地模型配置（本地知识库）", "test_local_model_config.py"),
    ("提示词安全条款（防注入/占位符/禁给分）", "test_prompt_guards.py"),
    ("版式/外观/分区顺序", "test_layout_options.py"),
    ("Agent 接线与生命周期", "test_agent_wiring.py"),
    ("Agent 会话列表（本机持久化）", "test_agent_sessions.py"),
    ("简历生成历史与版本链", "test_resume_history.py"),
    ("简历优化 ATS 前后对比", "test_resume_optimize_ats.py"),
    ("简历优化页就地建档入口（G7）", "test_resume_optimize_import.py"),
    ("面经文件/截图 OCR 导入", "test_interview_import.py"),
    ("投递看板拖拽改状态", "test_tracker_kanban.py"),
    ("投递统计图表（漏斗/环形）", "test_tracker_stats.py"),
    ("更新 SHA256 校验", "test_updater_sha256.py"),
    ("登录态安全存储", "test_secure_storage.py"),
    ("后端契约测试", "test_backend_contract.py"),
    ("增量更新（只下变化文件）", "test_updater_incremental.py"),
    ("导出后回读自检", "test_export_verify.py"),
    ("撤销栈与未保存草稿", "test_undo_draft.py"),
    ("模板筛选 / ATS 视角 / 分区顺序", "test_picker_filter.py"),
    ("主题跟随系统", "test_theme_system.py"),
    ("日历视图与 .ics 导出", "test_ics_export.py"),
    ("投递追踪日历 UI", "test_tracker_calendar_ui.py"),
    ("JSON Resume 互转（含官方 schema 校验）", "test_json_resume.py"),
    ("JSON Resume 导入导出 UI", "test_json_resume_ui.py"),
    ("生成守门层（编造/风险/AI腔/量化）", "test_generation_guard.py"),
    ("薪资基准刷新/导入（G8）", "test_salary_benchmark.py"),
    ("成本调用链下钻（G9）", "test_cost_trace_detail.py"),
    ("首页最近投递与匹配速览（G10）", "test_dashboard_glance.py"),
    ("简历生成/优化缺陷修复（守门取值·字段丢失·构造期请求·校验）",
     "test_resume_fix_round2.py"),
    ("本地判据层（充分度/质量/一页约束/bullet 保真/方向词表）",
     "test_ai_local_judges.py"),
    ("本地生成护栏（事实回填/编造剔除/数字兜底/一页 fail-closed/质量重试）",
     "test_ai_local_generate.py"),
    ("本地优化护栏（保真阈值/新增事实拦截/三刀裁剪/本地落库/ATS 前后对比）",
     "test_ai_local_optimize.py"),
    ("本地面试/匹配/薪资（会话状态机/开口价三档/本机基准数据/排行合并）",
     "test_ai_local_interview_salary_match.py"),
    ("多栏版式（双栏模板声明 / 真分栏 / 单栏回归）", "test_two_column_layout.py"),
    ("复用闭环（成果分流/质量闸门/回声防护/双池召回/自动入池）",
     "test_ai_local_retrieval_closure.py"),
    ("面试/薪资 GitHub 资产（内置题库兜底 / 4 维评分 / 市场定位 / 谈判打法库）",
     "test_interview_salary_assets.py"),
    ("薪资页「我手里的 offer → 算定位」接线（输入解析 / 档位文案 / 控件生命周期）",
     "test_salary_offer_locator_ui.py"),
]


def main() -> int:
    failed = []
    missing = []
    ran = 0
    for label, filename in TESTS:
        path = os.path.join(HERE, filename)
        if not os.path.exists(path):
            # ⚠️ 这里**不能**静默跳过：登记在案却找不到文件，说明清单与磁盘不一致，
            #    而下面若按 len(TESTS) 报数，就会出现「报告说全绿、那个套件其实没跑」
            #    的假绿。（2026-10-10 发现：core/skill_keywords.py 的 docstring 引用了一个
            #    并不存在的测试文件，因静默跳过而长期无感。）
            print("!!  %s 已登记但文件不存在：%s" % (label, filename))
            missing.append("%s (%s)" % (label, filename))
            continue
        print("=" * 60)
        print(">> %s  (%s)" % (label, filename))
        print("=" * 60)
        proc = subprocess.run([sys.executable, "-u", path], cwd=os.path.dirname(HERE))
        ran += 1
        if proc.returncode != 0:
            failed.append("%s (%s) 退出码 %d" % (label, filename, proc.returncode))

    print()
    print("=" * 60)
    if missing:
        print("测试清单里有 %d 个套件找不到文件（登记名与实际不符）：" % len(missing))
        for item in missing:
            print("  -", item)
    if failed:
        print("测试未全部通过：%d 项失败" % len(failed))
        for item in failed:
            print("  -", item)
    if failed or missing:
        return 1
    print("全部测试通过（%d/%d 项，全部实际执行）" % (ran, len(TESTS)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
