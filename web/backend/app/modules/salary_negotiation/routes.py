"""薪资谈判路由：开始谈判、逐轮回答、获取总结。"""
import logging

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.core.concurrency import generation_semaphore, run_sync
from app.core.database import get_db
from app.models.resume import Resume
from app.models.user import User
from app.modules.auth.routes import get_current_user_required
from app.modules.salary_negotiation.schemas import (
    BenchmarkImportRequest,
    SalaryReferenceRequest,
    StartNegotiationRequest,
    SubmitAnswerRequest,
)
from app.modules.salary_negotiation.services import (
    create_negotiation_session,
    get_benchmark_info,
    get_negotiation_session,
    get_negotiation_summary,
    get_salary_reference,
    import_benchmark,
    refresh_benchmark,
    submit_negotiation_answer,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/salary-negotiation", tags=["薪资谈判模块"])


@router.post(
    "/start",
    summary="开始薪资谈判",
    description="创建谈判会话，返回 HR 开场白",
)
async def start_negotiation(
    req: StartNegotiationRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_required),
):
    try:
        # create_negotiation_session 内含 LLM 生成 HR 开场白，丢线程池 + 过并发闸门
        async with generation_semaphore():
            session = await run_sync(
                create_negotiation_session,
                scenario=req.scenario,
                target=req.target_salary,
                bottom=req.bottom_salary,
                context=req.context,
                db=db,
                user_id=current_user.id,
                user=current_user,
            )
        logger.info("用户 %s 开始薪资谈判: session=%s scenario=%s", current_user.id, session.id, req.scenario)
        return {
            "success": True,
            "data": {
                "session_id": session.id,
                "hr_message": session.current_hr_message,
                "round_count": 1,
            },
            "error": None,
        }
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))


@router.post(
    "/reference",
    summary="薪资参考",
    description="根据简历估一个合理市场价（应届生起薪参考），数字由市场基准表计算",
)
async def salary_reference(
    req: SalaryReferenceRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_required),
):
    # 取简历文本：优先 resume_id，其次直接粘贴的文本
    resume_text = (req.resume_text or "").strip()
    if req.resume_id:
        resume = db.query(Resume).filter(
            Resume.id == req.resume_id,
            Resume.user_id == current_user.id,
        ).first()
        if not resume:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="简历不存在")
        resume_text = (resume.raw_text or "").strip()

    if not resume_text:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="请选择一份简历或粘贴简历内容",
        )

    # get_salary_reference 内含 LLM 简历画像+薪资解读，丢线程池 + 过并发闸门
    async with generation_semaphore():
        result = await run_sync(
            get_salary_reference,
            resume_text=resume_text,
            target_city=req.target_city,
            position_hint=req.position_hint,
            db=db,
            user=current_user,
        )
    return {"success": True, "data": result, "error": None}


@router.get(
    "/benchmark/info",
    summary="薪资基准数据版本信息",
    description="返回当前生效的市场薪资基准数据的版本、年份、来源、更新时间",
)
async def benchmark_info(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_required),
):
    return {"success": True, "data": get_benchmark_info(db), "error": None}


@router.get(
    "/benchmark/data",
    summary="薪资基准完整数据",
    description="返回当前生效的完整基准系数（学历基数/方向系数/城市档），供桌面版本地规则引擎使用",
)
async def benchmark_data(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_required),
):
    """返回生效版本的完整系数数据（只读）。

    背景：桌面版内置的薪资基准是出厂静态基线（v1），而服务器上可能已被
    refresh（模型联网刷新）或 import（导入权威调研）升级到更新版本。
    实测 2026-09-30：服务器生效 v2 的学历基数较内置基线普遍下调，
    若不拉取，桌面算出的薪资区间会偏高。故此接口供桌面版同步权威数据。
    """
    from app.core.salary_benchmark_store import active_data

    return {"success": True, "data": active_data(db), "error": None}


@router.post(
    "/benchmark/refresh",
    summary="刷新市场薪资基准数据",
    description="调用模型基于最新行情刷新基准数据，写入新版本（旧版本保留可回滚）",
)
async def benchmark_refresh(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_required),
):
    try:
        # refresh_benchmark 内含 LLM 联网刷新基准，丢线程池 + 过并发闸门
        async with generation_semaphore():
            data = await run_sync(refresh_benchmark, db)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    logger.info("用户 %s 触发薪资基准数据刷新，当前版本 v%s", current_user.id, data.get("version"))
    return {"success": True, "data": get_benchmark_info(db), "error": None}


@router.post(
    "/benchmark/import",
    summary="导入权威市场薪资数据",
    description="导入权威调研数据（JSON）覆盖模型估算，作为新的生效版本",
)
async def benchmark_import(
    req: BenchmarkImportRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_required),
):
    try:
        data = import_benchmark(db, req.data, req.data_year, req.source, req.note)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    logger.info("用户 %s 导入薪资基准数据，当前版本 v%s", current_user.id, data.get("version"))
    return {"success": True, "data": get_benchmark_info(db), "error": None}


@router.post(
    "/{session_id}/answer",
    summary="提交谈判回应",
    description="提交候选人的回应，返回教练点评和 HR 下一句",
)
async def submit_answer(
    session_id: str,
    req: SubmitAnswerRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_required),
):
    session = get_negotiation_session(session_id, db, current_user.id)
    if not session:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="会话不存在")
    if session.status == "finished":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="谈判已结束")

    try:
        # submit_negotiation_answer 内含 LLM 教练点评+HR 下一句，丢线程池 + 过并发闸门
        async with generation_semaphore():
            coaching, next_hr, is_finished = await run_sync(
                submit_negotiation_answer,
                session_id=session_id,
                answer=req.answer,
                db=db,
                user_id=current_user.id,
                user=current_user,
            )
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))

    if coaching is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="会话不存在")

    session = get_negotiation_session(session_id, db, current_user.id)
    return {
        "success": True,
        "data": {
            "coaching": coaching,
            "next_hr_message": next_hr,
            "is_finished": is_finished,
            "round_count": session.round_count if session else 0,
        },
        "error": None,
    }


@router.get(
    "/{session_id}/summary",
    summary="获取谈判总结",
    description="获取整场谈判的总结、教练点评和完整对话",
)
async def get_summary(
    session_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_required),
):
    # get_negotiation_summary 内含 LLM 生成总结，丢线程池 + 过并发闸门
    async with generation_semaphore():
        summary = await run_sync(get_negotiation_summary, session_id, db, current_user.id, current_user)
    if not summary:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="会话不存在")
    return {"success": True, "data": summary, "error": None}
