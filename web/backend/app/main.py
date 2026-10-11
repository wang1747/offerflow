import asyncio
import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager
from app.core.exceptions import register_exception_handlers
from app.core.security import hash_password
from app.core import salary_benchmark_store as benchmark_store
from app.modules.application.reminder_push_service import push_reminders_for_user
from app.modules.application import router as application_router
from app.modules.admin import router as admin_router
from app.modules.auth import router as auth_router
from app.modules.career_agent.routes import router as career_agent_router
from app.modules.community import router as community_router
from app.modules.corpus import router as corpus_router
from app.modules.feedback import router as feedback_router
from app.modules.interview import router as interview_router
from app.modules.jd import router as jd_router
from app.modules.match import router as match_router
from app.modules.model_config import router as model_config_router
from app.modules.observability import router as observability_router
from app.modules.resume import router as resume_router
from app.modules.resume_generation import router as resume_generation_router
from app.modules.salary_negotiation import router as salary_negotiation_router
from app.modules.permissions import ROLE_ADMIN
from app.modules.parse import router as parse_router
from app.modules.integrations import router as integrations_router

from .config import get_settings
from .core.database import init_db, SessionLocal
from .models.user import User


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()

    # Agent 运行时：按 DATABASE_URL 选择 Checkpointer。
    # 生产（Postgres）= PostgresSaver：跨重启能恢复会话，``interrupt()`` 的断点
    # 也因此能跨重启恢复；sqlite/测试环境 = InMemorySaver。
    # 初始化失败**不阻断启动**（Agent 是增量能力），但会在日志与每个会话响应的
    # ``checkpointer`` 字段里如实表现为 memory —— 不静默假装已持久化。
    try:
        from .modules.career_agent.runtime import init_agent_runtime
        _cp_kind = await init_agent_runtime()
        logging.getLogger(__name__).info("Agent Checkpointer 已就绪：%s", _cp_kind)
    except Exception as e:  # noqa: BLE001
        logging.getLogger(__name__).warning(
            "Agent 运行时初始化失败（将退回内存 Checkpointer）：%s", e,
        )

    settings = get_settings()
    db = SessionLocal()
    try:
        default_user = db.query(User).filter(User.id == settings.default_user_id).first()
        if not default_user:
            default_user = User(id=settings.default_user_id, name=settings.default_user_name,
                                role=ROLE_ADMIN, allow_corpus=False)
            db.add(default_user)
            db.commit()
            print("[OK] 默认用户已创建（管理员）")
        elif default_user.name != settings.default_user_name:
            default_user.name = settings.default_user_name
            db.commit()

        # 默认用户兜底为管理员角色（历史数据可能是 user，需纠正）
        if default_user.role != ROLE_ADMIN:
            default_user.role = ROLE_ADMIN
            db.commit()
            print("[OK] 默认用户已提权为管理员")

        if not default_user.hashed_password:
            default_user.hashed_password = hash_password(settings.default_user_password)
            db.commit()
            print("[OK] 默认用户密码已初始化")

        try:
            from app.seed import seed_demo_data
            seed_demo_data(db)
        except Exception as e:
            print(f"[ERROR] 种子数据生成失败: {e}")

        try:
            benchmark_store.seed_default(db)
            print("[OK] 薪资基准数据已初始化")
        except Exception as e:
            print(f"[ERROR] 薪资基准数据 seed 失败: {e}")
    finally:
        db.close()

    reminder_task = None
    if settings.reminder_webhook_url:
        async def reminder_loop():
            while True:
                await asyncio.sleep(settings.reminder_check_interval_minutes * 60)
                reminder_db = SessionLocal()
                try:
                    users = reminder_db.query(User).all()
                    for user in users:
                        try:
                            await push_reminders_for_user(
                                webhook_url=settings.reminder_webhook_url,
                                db=reminder_db,
                                user=user,
                            )
                        except Exception as e:
                            logging.getLogger(__name__).error("提醒推送失败: %s", e)
                finally:
                    reminder_db.close()

        reminder_task = asyncio.create_task(reminder_loop())

    # 薪资基准数据定期刷新任务（默认每 30 天，可配置；0 表示禁用自动刷新）
    benchmark_refresh_task = None
    if settings.salary_benchmark_refresh_days > 0:
        async def benchmark_refresh_loop():
            from datetime import datetime
            while True:
                await asyncio.sleep(6 * 3600)  # 每 6 小时检查一次是否到刷新周期
                refresh_db = SessionLocal()
                try:
                    active = benchmark_store.active_data(refresh_db)
                    updated_at = active.get("updated_at")
                    if updated_at:
                        try:
                            last = datetime.fromisoformat(str(updated_at).replace("Z", "+00:00"))
                            now = datetime.now(last.tzinfo) if last.tzinfo else datetime.now()
                            if (now - last).days < settings.salary_benchmark_refresh_days:
                                continue  # 未到刷新周期
                        except (ValueError, TypeError):
                            pass
                    from app.core.concurrency import run_sync
                    from app.modules.salary_negotiation.services import refresh_benchmark
                    # refresh_benchmark 内含 LLM 联网刷新，丢线程池避免占死事件循环
                    await run_sync(refresh_benchmark, refresh_db)
                    logging.getLogger(__name__).info("薪资基准数据已自动刷新")
                except Exception as e:
                    logging.getLogger(__name__).error("薪资基准数据自动刷新失败: %s", e)
                finally:
                    refresh_db.close()

        benchmark_refresh_task = asyncio.create_task(benchmark_refresh_loop())

    yield

    # Agent 运行时：关闭 Checkpointer 连接池（PG 路径才有资源要放）
    try:
        from .modules.career_agent.runtime import shutdown_agent_runtime
        await shutdown_agent_runtime()
    except Exception as e:  # noqa: BLE001
        logging.getLogger(__name__).warning("关闭 Agent 运行时失败: %s", e)

    if reminder_task:
        reminder_task.cancel()
    if benchmark_refresh_task:
        benchmark_refresh_task.cancel()


app = FastAPI(
    title="OfferFlow API",
    version="1.0.0",
    lifespan=lifespan,
)

settings = get_settings()
allowed_origins = [origin.strip() for origin in settings.cors_origins.split(",") if origin.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(jd_router, prefix="/api/v1/jd", tags=["JD解析模块"])
app.include_router(resume_router, prefix="/api/v1/resume", tags=["简历模块"])
app.include_router(resume_generation_router, prefix="/api/v1/resume-generation", tags=["简历生成模块"])
app.include_router(match_router)
app.include_router(interview_router)
app.include_router(salary_negotiation_router)
app.include_router(application_router)
app.include_router(auth_router)
app.include_router(model_config_router)
app.include_router(admin_router)
app.include_router(observability_router)
app.include_router(corpus_router, prefix="/api/v1/corpus", tags=["语料库模块"])
app.include_router(parse_router, prefix="/api/v1/parse", tags=["文档解析与图谱"])
app.include_router(integrations_router, prefix="/api/v1/integrations", tags=["集成能力聚合"])
app.include_router(community_router)
app.include_router(feedback_router)
app.include_router(career_agent_router)

register_exception_handlers(app)

@app.get("/api/health")
async def health_check():
    return {"success": True, "data": {"status": "ok", "message": "OfferFlow is running"}, "error": None}
