# -*- coding: utf-8 -*-
"""扩充语料库：覆盖 12 个方向 × {简历范文, JD, 面经} 的高质量种子语料。

用法（在 backend 容器内）：
    python scripts/import_corpus_rich.py
会自动按 salary_benchmark 的 19 大类关键词做方向分类 + 技能词打 tags + embedding。
"""
import os
import sys

# 兼容两种运行位置：本地 scripts/ 下（app 在 ../backend）、容器内 /app 下（app 就在当前）
_HERE = os.path.dirname(os.path.abspath(__file__))
for _p in (_HERE, os.path.dirname(_HERE), os.path.join(_HERE, "..", "backend")):
    if os.path.isdir(os.path.join(_p, "app")):
        sys.path.insert(0, _p)
        break

from app.core.database import init_db, SessionLocal
from app.modules.corpus.services import add_corpus_item

# ============ 简历范文（item_type=resume） ============
RESUME_SAMPLES = [
    # ---- AI / 算法 / 大模型 ----
    "张三，985 计算机硕士，方向 AI 算法工程师。掌握 Python、PyTorch、TensorFlow，熟悉机器学习、深度学习、大模型微调（LoRA/P-Tuning）。实习经历：在某大厂 AI 团队参与大语言模型对话系统开发，负责 RAG 检索增强与向量数据库搭建，将问答准确率提升 18%。项目：基于 BERT 的文本分类、基于 LangChain 的智能客服。熟悉 Transformer、NLP、embedding。",
    "李四，计算机本科，方向机器学习算法。熟练掌握机器学习、深度学习、计算机视觉，熟悉 OpenCV、PyTorch。项目：YOLO 目标检测模型部署、人脸识别系统。实习：参与推荐算法优化，CTR 提升 6%。熟悉 Python、C++、数据结构与算法。",
    # ---- 后端开发 ----
    "王五，软件工程本科，方向 Java 后端开发。掌握 Java、Spring Boot、Spring Cloud、MySQL、Redis、消息队列 Kafka。实习：电商平台订单系统开发，负责分布式事务与缓存优化，接口 QPS 提升 40%。熟悉微服务、Docker、Linux。项目：秒杀系统、权限管理平台。",
    "赵六，计算机本科，方向 Python 后端开发。掌握 Python、FastAPI、Django、PostgreSQL、Redis。实习：SaaS 后台开发，负责 RESTful API 设计与异步任务调度，熟悉 Celery。项目：爬虫数据采集平台、REST 接口网关。熟悉 Docker、Nginx、Linux。",
    # ---- 前端开发 ----
    "孙七，软件工程本科，方向前端开发。掌握 React、TypeScript、Vue3、Webpack/Vite。实习：负责中后台管理系统的组件库封装与性能优化，首屏加载时间降低 35%。熟悉 HTTP、浏览器渲染原理、跨域。项目：低代码表单生成器、可视化大屏。",
    "周八，计算机本科，方向前端开发。掌握 Vue3、TypeScript、Element Plus、ECharts。实习：参与数据可视化平台开发，负责图表组件与状态管理。熟悉 Axios、Vue Router、Pinia。项目：在线教育平台、商城前端。",
    # ---- 全栈开发 ----
    "吴九，计算机本科，方向全栈开发。掌握 React + Node.js + PostgreSQL，熟悉 Next.js、NestJS。实习：全栈开发校园二手交易平台，独立完成前后端与部署。熟悉 RESTful、JWT、Docker。项目：博客系统、在线协作文档。",
    # ---- 测试 / QA ----
    "郑十，计算机本科，方向测试开发。掌握 Python、Pytest、Selenium、Postman、JMeter。实习：负责 Web 端接口自动化测试与性能测试，搭建 CI 流水线，缺陷率下降 25%。熟悉接口测试、UI 自动化、测试用例设计。",
    # ---- 运维 / DevOps / SRE ----
    "陈一，计算机本科，方向运维开发。掌握 Linux、Docker、Kubernetes、CI/CD（GitLab/Jenkins）、Prometheus。实习：负责容器化改造与监控告警体系搭建，部署效率提升 50%。熟悉 Shell、Python、Nginx、Ansible。",
    # ---- 大数据 / 数据开发 ----
    "林二，计算机本科，方向大数据开发。掌握 Hadoop、Spark、Flink、Hive、Kafka。实习：负责离线数仓 ETL 开发与数据质量监控。熟悉 SQL、Scala、数据仓库分层建模。项目：实时用户行为分析平台。",
    # ---- 数据分析 ----
    "黄三，统计学本科，方向数据分析。掌握 Python、SQL、Excel、Tableau、PowerBI。实习：负责业务数据看板搭建与 AB 测试分析，输出增长建议带动转化率提升 12%。熟悉 pandas、NumPy、数据可视化。",
    # ---- 产品经理 ----
    "何四，计算机本科，方向产品经理。熟悉产品全流程：需求分析、原型设计、PRD 撰写、数据分析。实习：负责 B 端 SaaS 产品的功能迭代，通过用户访谈与数据分析定义需求，DAU 提升 20%。熟练使用 Axure、Figma、SQL。",
    # ---- UI/UX 设计 ----
    "冯五，视觉传达本科，方向 UI/UX 设计师。熟练使用 Figma、Sketch、PS、AI。实习：负责 App 界面设计与设计规范搭建，主导改版后用户满意度提升。熟悉设计系统、交互原型、动效设计。项目：电商 App 改版、B 端后台设计。",
    # ---- 运营 / 新媒体 ----
    "蒋六，新闻学本科，方向内容运营。熟悉新媒体运营、公众号、小红书、抖音内容策划。实习：负责账号内容创作与增长，单篇阅读量 10w+，粉丝增长 5 万。熟悉数据分析、选题策划、文案撰写。",
    # ---- 金融 / 财会 ----
    "沈七，会计学本科，方向财务/会计。掌握财务核算、成本管理、税务申报，熟悉用友/金蝶、Excel 高级函数。实习：负责费用报销审核与凭证编制，参与月度结账。熟悉财务报表分析、内部控制。",
]

# ============ JD（item_type=jd） ============
JD_SAMPLES = [
    "【AI 算法工程师】本科以上，计算机/AI 相关专业。熟悉 Python、PyTorch、TensorFlow，掌握机器学习、深度学习、大模型、NLP、Transformer。有 LLM 微调、RAG、向量检索经验优先。负责大模型应用开发与算法优化。",
    "【机器学习算法工程师】本科以上。熟悉机器学习、深度学习、计算机视觉，掌握 OpenCV、PyTorch。有目标检测、图像分类项目经验。负责算法模型训练、部署与优化。",
    "【Java 后端开发工程师】本科以上。熟悉 Java、Spring Boot、Spring Cloud、MySQL、Redis、消息队列。了解微服务、分布式、高并发。负责后端系统设计与开发。",
    "【Python 后端开发工程师】本科以上。熟悉 Python、FastAPI/Django、PostgreSQL、Redis、Celery。了解 Docker、Linux。负责 API 服务与数据处理开发。",
    "【前端开发工程师】本科以上。熟悉 React/Vue、TypeScript、Webpack/Vite。了解浏览器原理、性能优化、HTTP。负责前端页面与组件开发。",
    "【前端开发工程师(Vue)】本科以上。熟悉 Vue3、TypeScript、Element Plus、ECharts。有中后台、数据可视化经验优先。负责前端功能开发与优化。",
    "【全栈开发工程师】本科以上。熟悉 React/Node.js 或 Vue/Java，掌握一种关系型数据库。了解 Docker、云部署。负责前后端全流程开发。",
    "【测试开发工程师】本科以上。熟悉 Python、Pytest、Selenium、Postman、JMeter。有接口自动化、性能测试经验。负责质量保障与自动化测试建设。",
    "【运维开发/SRE 工程师】本科以上。熟悉 Linux、Docker、Kubernetes、CI/CD、Prometheus。有容器化、监控告警经验。负责系统稳定性与自动化运维。",
    "【大数据开发工程师】本科以上。熟悉 Hadoop、Spark、Flink、Hive、Kafka。了解数据仓库、ETL。负责实时/离线数据开发。",
    "【数据分析师】本科以上，统计/数学/计算机优先。熟悉 SQL、Python、Excel、Tableau。有 AB 测试、数据看板经验。负责业务数据分析与洞察。",
    "【产品经理】本科以上。熟悉产品需求分析、原型设计、PRD 撰写、数据分析。熟练 Axure/Figma。有 B 端/C 端产品经验优先。负责产品规划与迭代。",
    "【UI/UX 设计师】本科以上，设计相关专业。熟练 Figma、Sketch、PS、AI。有设计系统、交互原型经验。负责产品界面与体验设计。",
    "【内容/新媒体运营】本科以上。熟悉公众号、小红书、抖音等内容平台，有选题策划、文案撰写、数据分析能力。负责内容创作与账号增长。",
    "【财务/会计】本科以上，财务相关专业。熟悉财务核算、成本、税务，熟练用友/金蝶与 Excel。负责账务处理与报表编制。",
]

# ============ 面经（item_type=interview） ============
INTERVIEW_SAMPLES = [
    "字节跳动 AI 算法岗面经：一面问项目细节和 Transformer 原理、注意力机制、为什么用 RAG；二面手撕算法题（链表反转、最长无重复子串）和大模型微调 Loss 曲线；三面聊项目难点、向量检索优化。整体考察深度和工程落地能力。",
    "腾讯机器学习面经：重点问深度学习基础（反向传播、过拟合）、推荐系统项目、排序模型；算法题考了二叉树层序遍历和 LRU。面试官很看重数学基础和工程思维。",
    "阿里 Java 后端面经：一面 Java 基础（HashMap、并发）、MySQL 索引和事务；二面项目里秒杀系统的架构、Redis 缓存穿透/雪崩；三面分布式一致性、消息队列。算法考了快速排序和两数之和。",
    "字节 Python 后端面经：项目深挖 FastAPI 异步原理、Celery 任务队列；考了数据库索引优化、Redis 数据结构；算法题是反转链表和爬楼梯。重视基础扎实和项目真实性。",
    "美团前端面经：一面 JS 闭包、事件循环、原型链；二面 React 性能优化、虚拟 DOM、Hooks 原理；三面项目里组件库封装。手写题考了防抖节流和深拷贝。",
    "字节前端面经：考了 TypeScript 类型、浏览器渲染流程、跨域解决方案、webpack 打包优化；算法题是合并两个有序数组。面试节奏快，重视框架原理。",
    "大厂测试开发面经：一面 Python 基础、接口测试、Pytest 框架；二面自动化测试设计、性能测试工具 JMeter；三面质量体系建设思路。手写题考了单例模式。",
    "运维开发面经：重点问 Linux 常用命令、Docker 原理、K8s 组件、CI/CD 流程；场景题考了线上故障排查思路和监控告警设计。重视实战经验。",
    "大数据开发面经：问 Spark 与 Flink 区别、数据倾斜解决方案、数仓分层；SQL 题考了窗口函数。算法考了 TopK。考察 Hive 调优经验。",
    "数据分析面经：重点问 SQL 窗口函数、AB 测试原理、指标体系搭建；场景题考了如何定位某指标下降。Excel 和 Python pandas 也会考。",
    "产品经理面经：一面产品思维、需求优先级排序；二面深挖实习项目、数据分析；三面聊职业规划和产品判断。重视逻辑表达和用户洞察。",
    "UI/UX 设计面经：考察作品集讲解、设计思路、设计规范；笔试让分析一个 App 的体验问题并给改版方案。重视设计审美和用户体验思维。",
    "新媒体运营面经：问选题策划思路、爆款内容方法论、账号增长案例；考了数据分析（阅读量、转化率）。重视网感和执行力。",
    "财务岗面经：问财务核算流程、三大报表勾稽关系、税务基础；Excel 实操考了 VLOOKUP 和透视表。重视专业基础和细心。",
]

ALL_CORPUS = (
    [("resume", t) for t in RESUME_SAMPLES]
    + [("jd", t) for t in JD_SAMPLES]
    + [("interview", t) for t in INTERVIEW_SAMPLES]
)


def main():
    init_db()
    db = SessionLocal()
    imported = 0
    try:
        for item_type, raw in ALL_CORPUS:
            try:
                add_corpus_item(item_type=item_type, raw_text=raw, db=db, source="import", auto_tag=True)
                imported += 1
            except Exception as e:  # noqa: BLE001
                print(f"[跳过] {e}")
        print(f"本次导入 {imported} 条语料")
        from app.modules.corpus.services import count_corpus
        print("语料库统计:", count_corpus(db))
    finally:
        db.close()


if __name__ == "__main__":
    main()
