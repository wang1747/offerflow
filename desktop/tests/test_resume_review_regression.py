# -*- coding: utf-8 -*-
"""补齐 6 项规则后的回归测试：好简历不掉分、差简历被更准确命中、每条新规则都能触发。"""
import os
import sys

# 仓库内定位：tests/ 的父目录即 desktop/（offerflow_desktop 包所在）。
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from offerflow_desktop.core.resume_review import review_text  # noqa: E402

JD = """岗位：后端开发工程师
要求：
1. 熟悉 Python / Java，掌握 FastAPI、Django 等框架
2. 熟悉 MySQL、Redis，了解缓存与索引优化
3. 有 Docker、K8s 部署经验
4. 熟悉 RAG、LLM 相关技术优先
5. 具备高并发、分布式系统经验
"""

GOOD = """张三
138-0000-0000 | wang@example.com | github.com/wang

求职意向
后端开发工程师（Python）

个人总结
后端开发方向应届生，主攻 Python 服务端开发与检索链路调优。独立从 0 到 1 搭建过 OfferFlow 求职工作台桌面应用并交付 2 个版本，熟悉 FastAPI、MySQL、Redis 与 Docker 部署，主导接口性能优化使响应时间从 800ms 降到 120ms。

教育经历
北京信息科技大学 计算机科学与技术 本科 2023.09 – 2027.06
主修数据结构、操作系统、数据库原理，绩点 3.7/4.0（专业前 15%）

项目经历
OfferFlow 求职工作台（独立开发） 2026.03 – 至今
- 主导桌面端架构设计，基于 PySide6 完成 UI 与业务分层，交付 2 个可用版本
- 重构简历导出模块，用 python-docx 实现 A4 版式与时间右对齐，版式错误率降为 0
- 引入 BM25 与向量混合检索并经 RRF 融合，检索 MRR 从 0.786 提升到 1.0
- 搭建内容质检规则引擎，沉淀 28 条简历规范，问题召回率提升 45%
- 设计 SQLite 多版本历史，通过 parent_id 关联版本链，支持 v1 到 v3 追溯

实习经历
某科技公司 后端开发实习生 2025.07 – 2025.09
- 开发订单查询接口并对慢 SQL 加二级索引，接口响应从 800ms 降到 120ms
- 设计 30+ 条边界用例并推动修复，上线后回归缺陷下降 40%
- 搭建 Docker 部署流水线，发布耗时从 15 分钟缩短到 3 分钟

技能
语言：Python、Java
框架与库：FastAPI、Django、PySide6
工具与平台：Docker、K8s、MySQL、Redis、Git

证书
软件设计师
"""

BAD = """张三
12345678901

个人总结
本人性格开朗，吃苦耐劳，有责任心，学习能力强，希望找到一份好工作。

项目经历
做了个网站
- 负责前端页面开发
- 参与了后端接口
- 协助测试

教育经历
某大学 计算机
技能
Java、Java、Python
"""


def show(title, text, jd="", limit=14):
    r = review_text(text, jd)
    s = r["stats"]
    print(f"\n===== {title} | 得分 {r['score']}（{r['level']}） =====")
    print(f"  字数 {s['char_count']}｜要点 {s['bullet_count']}｜量化率 {s['quantified_ratio']:.0%}"
          f"｜弱动词 {s['weak_verb_count']}｜套话 {s['cliche_hits']}｜模糊 {s['vague_hits']}")
    for hit in r["issues"][:limit]:
        print(f"  [{hit['severity']:<6}] {hit['category']}｜{hit['location']}：{hit['message']}")
    for line in r["highlights"]:
        print(f"  OK  {line}")
    return r


print("==================== 基线：好 / 差简历（不带 JD） ====================")
show("规范简历（期望仍然高分）", GOOD)
show("故意写差的简历（期望更低分）", BAD)

print("\n==================== 带 JD：相关性排序 / 堆砌 ====================")
show("规范简历 + JD（期望不掉分）", GOOD, JD)

C8_CASE = """李四
13800000000 | li@example.com

个人总结
后端开发方向应届生，熟悉 Python 与 FastAPI，主导过接口优化使响应时间降低 60%，独立完成 2 个服务模块。

项目经历
校园社团管理系统 2025.03 – 2025.06
- 主导社团活动报名流程设计，覆盖 3 个学院的 500 名学生，报名效率提升 50%

后端服务项目 2025.07 – 2025.10
- 搭建 FastAPI 服务并接入 MySQL、Redis 缓存，接口响应从 900ms 降到 150ms
- 用 Docker 与 K8s 部署上线，支撑 2000 并发，错误率降低 80%

技能
语言：Python、Java、SQL
框架与库：FastAPI、Django、Redis
工具与平台：MySQL、Docker、K8s、Git
"""
show("C8 首段与 JD 无关、后段高度相关（期望触发排序提示）", C8_CASE, JD)

C11_CASE = """王五
13800000000 | w@example.com

个人总结
Python 开发方向，主攻 Python 后端与 Python 数据处理，用 Python 独立交付过 2 个项目。

项目经历
Python 数据平台 2025.03 – 2025.06
- 用 Python 搭建 Python 数据管道，Python 脚本处理 300 万条记录，耗时缩短 70%

技能
语言：Python、Python、Python
"""
show("C11 同一关键词反复出现（期望触发堆砌提示）", C11_CASE, JD)

print("\n==================== 逐条规则定点验证 ====================")
C1_CASE = """赵六
13800000000 | z@example.com

个人总结
数据开发方向应届生，熟悉 Python 与 SQL，独立完成 3 个数据管道项目，查询耗时降低 50%。

项目经历
数据平台项目 2025.03 – 2025.06
- 整理会议记录并汇报进度
- 搭建数据管道并优化 SQL 查询，处理耗时从 20 分钟降到 6 分钟
- 设计对账脚本，覆盖 12 张表的日终校验，差错率降为 0

技能
语言：Python、SQL
框架与库：Pandas、NumPy
工具与平台：MySQL、Git、Linux
"""
r1 = show("C1 第 1 条要点既无方法也无结果（期望命中「只见动作」）", C1_CASE)

C3_CASE = """钱七
13800000000 | q@example.com

个人总结
前端开发方向应届生，熟悉 Vue 与 TypeScript，独立完成 2 个管理系统，首屏加载时间降低 55%。

项目经历
管理后台项目 2025.03 – 2025.06
- 重构列表页虚拟滚动并引入图片懒加载与请求缓存，首屏渲染时间从 3.2 秒降到 1.1 秒，同时把长列表滚动卡顿率降低到几乎不可感知的水平，兼顾了低端机型的兼容性表现
- 用 Vue3 开发
- 设计组件库并沉淀 20 个通用组件，复用率提升 60%

技能
语言：TypeScript、JavaScript
框架与库：Vue、Pinia
工具与平台：Vite、Git、Figma
"""
r3 = show("C3 第 1 条超长、第 2 条过短（期望分别命中）", C3_CASE, limit=20)

C4_CASE = """孙八
13800000000 | s@example.com

个人总结
测试开发方向应届生，熟悉 Pytest 与接口自动化，独立搭建测试框架，回归耗时降低 65%。

项目经历
接口自动化项目 2025.03 – 2025.06
- 搭建 Pytest 自动化框架并覆盖 120 条接口用例，回归耗时从 4 小时降到 1.4 小时
- 设计缺陷分级机制并推动修复，线上漏测率降低 30%

技能
语言：Python、SQL
框架与库：Pytest、Requests
工具与平台：Jenkins、Git、Docker
"""
r4 = show("C4 每段仅 2 条要点（期望命中「略少于 3 条」）", C4_CASE)

C10_CASE = """周九
13800000000 | zh@example.com

个人总结
计算机科学与技术专业应届生，主攻后端开发方向，熟悉 Python、FastAPI 与 MySQL，具备规范的编码习惯。

项目经历
算法项目 2025.03 – 2025.06
- 搭建推荐模型并引入特征分桶，CTR 提升 18%
- 设计离线评估流程，覆盖 6 个场景，召回率提升 12%

技能
语言：Python
框架与库：PyTorch
工具与平台：Linux、Git、Docker
"""
r10 = show("C10 个人总结够长但缺代表成果（期望命中）", C10_CASE)

print("\n==================== 断言 ====================")
assert r3["score"] < 100 and any("超上限" in i["message"] or "过短" in i["message"] for i in r3["issues"]), "C3 未触发"
assert any("略少于" in i["message"] for i in r4["issues"]), "C4 未触发"
assert any("只见动作" in i["message"] for i in r1["issues"]), "C1 未触发"
assert any("代表性成果" in i["message"] for i in r10["issues"]), "C10 未触发"
good = review_text(GOOD)
assert good["score"] >= 95, f"好简历掉分过低：{good['score']}"
bad = review_text(BAD)
assert bad["score"] <= 55, f"差简历分偏高：{bad['score']}"
print("\nALL OK")
