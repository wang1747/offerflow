"""
种子数据 - 用于快速演示
"""

from datetime import datetime, timedelta
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models.user import User
from app.models.jd import JobDescription
from app.models.resume import Resume
from app.models.application import Application
from app.models.application import ApplicationEvent
from app.models.interview import InterviewArticle, InterviewQuestion


def seed_demo_data(db: Session):
    """生成演示数据"""
    settings = get_settings()
    user_id = settings.default_user_id

    # 检查是否已有数据
    existing = db.query(JobDescription).filter(JobDescription.user_id == user_id).first()
    if existing:
        print("[SKIP] 数据库已有数据，跳过种子填充")
        return

    print("[INFO] 开始生成种子数据...")

    # 1. JD 数据
    jds = [
        JobDescription(
            user_id=user_id,
            raw_text="字节跳动招聘Python后端开发工程师，要求本科以上学历，3年以上Python经验，熟悉Django、MySQL、Redis，有高并发系统开发经验优先",
            company="字节跳动",
            position="Python后端开发工程师",
            must_have=["本科以上学历", "3年以上Python经验", "熟悉Django"],
            nice_to_have=["高并发系统开发经验", "熟悉Redis"],
            tech_stack={"backend": ["Python", "Django"], "database": ["MySQL", "Redis"]},
            hidden_signals=["高并发场景", "团队规模大"]
        ),
        JobDescription(
            user_id=user_id,
            raw_text="腾讯招聘前端开发工程师，要求本科以上学历，2年以上React经验，熟悉TypeScript、Webpack，有性能优化经验优先",
            company="腾讯",
            position="前端开发工程师",
            must_have=["本科以上学历", "2年以上React经验", "熟悉TypeScript"],
            nice_to_have=["性能优化经验"],
            tech_stack={"frontend": ["React", "TypeScript", "Webpack"]},
            hidden_signals=["大厂", "晋升通道清晰"]
        ),
        JobDescription(
            user_id=user_id,
            raw_text="阿里巴巴招聘Java开发工程师，要求本科以上学历，3年以上Java经验，熟悉Spring Boot、微服务架构，有分布式系统经验优先",
            company="阿里巴巴",
            position="Java开发工程师",
            must_have=["本科以上学历", "3年以上Java经验", "熟悉Spring Boot"],
            nice_to_have=["分布式系统经验"],
            tech_stack={"backend": ["Java", "Spring Boot", "微服务"]},
            hidden_signals=["双11备战", "技术氛围浓厚"]
        ),
    ]
    for jd in jds:
        db.add(jd)
    db.commit()

    # 2. 简历数据
    resumes = [
        Resume(
            user_id=user_id,
            version=1,
            raw_text="张三，计算机专业本科，5年Python后端开发经验，精通Django、FastAPI，熟悉MySQL、Redis，主导过电商平台后端架构开发",
            source_file="zhangsan_resume.txt"
        ),
        Resume(
            user_id=user_id,
            version=1,
            raw_text="李四，软件工程硕士，3年Java开发经验，熟悉Spring Boot、Spring Cloud，有微服务项目实践经历",
            source_file="lisi_resume.txt"
        ),
    ]
    for resume in resumes:
        db.add(resume)
    db.commit()

    # 3. 面经数据
    articles = [
        InterviewArticle(
            user_id=user_id,
            company="字节跳动",
            position="Python后端开发",
            raw_content="面试官问了项目架构、高并发处理、数据库索引优化，全程45分钟",
            questions=["请介绍你的项目架构", "如何处理高并发请求", "数据库索引优化策略"],
            difficulty="中等",
            source="manual"
        ),
        InterviewArticle(
            user_id=user_id,
            company="腾讯",
            position="前端开发",
            raw_content="面试重点在React性能优化、渲染原理、设计模式",
            questions=["React渲染原理", "性能优化手段", "设计模式应用"],
            difficulty="中等",
            source="manual"
        ),
    ]
    for article in articles:
        db.add(article)
    db.commit()

    # 4. 投递数据
    now = datetime.now()
    applications = [
        Application(
            user_id=user_id,
            company="字节跳动",
            position="Python后端开发工程师",
            status="first_interview",
            jd_id=jds[0].id,
            resume_id=resumes[0].id,
            applied_date=now - timedelta(days=5),
            next_action="准备技术面",
            next_action_date=now + timedelta(days=2),
            notes="一面已过，准备二面"
        ),
        Application(
            user_id=user_id,
            company="腾讯",
            position="前端开发工程师",
            status="applied",
            jd_id=jds[1].id,
            resume_id=resumes[1].id,
            applied_date=now - timedelta(days=1),
            next_action="等待初筛",
            next_action_date=now + timedelta(days=5),
            notes="刚投递"
        ),
        Application(
            user_id=user_id,
            company="阿里巴巴",
            position="Java开发工程师",
            status="saved",
            jd_id=jds[2].id,
            notes="暂未投递，先收藏"
        ),
    ]
    for app in applications:
        db.add(app)
    db.commit()

    # 5. 投递事件
    events = [
        ApplicationEvent(
            application_id=applications[0].id,
            event_type="status_change",
            from_status="applied",
            to_status="first_interview",
            description="通过简历筛选，进入一面",
            event_date=now - timedelta(days=3)
        ),
        ApplicationEvent(
            application_id=applications[1].id,
            event_type="status_change",
            from_status="saved",
            to_status="applied",
            description="已投递简历",
            event_date=now - timedelta(days=1)
        ),
    ]
    for event in events:
        db.add(event)
    db.commit()

    print("[OK] 种子数据生成完成")
    print(f"   - {len(jds)} 条 JD")
    print(f"   - {len(resumes)} 份简历")
    print(f"   - {len(articles)} 篇面经")
    print(f"   - {len(applications)} 条投递记录")
