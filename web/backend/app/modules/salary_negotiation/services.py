"""薪资谈判服务：会话创建、逐轮谈判、教练反馈、总结、薪资参考、基准数据刷新。"""
import logging
from typing import Optional, Tuple

from sqlalchemy.orm import Session

from app.agents.graphs.salary_negotiation import (
    coach_answer,
    generate_hr_message,
    generate_negotiation_summary,
)
from app.agents.graphs.salary_reference import (
    extract_resume_profile,
    generate_salary_analysis,
)
from app.core import salary_benchmark as benchmark
from app.core import salary_benchmark_store as benchmark_store
from app.models.negotiation import NegotiationSession
from app.models.user import User

logger = logging.getLogger(__name__)

MAX_ROUNDS = 5


def create_negotiation_session(
    scenario: str,
    target: Optional[str],
    bottom: Optional[str],
    context: str,
    db: Session,
    user_id: str,
    user: User,
) -> NegotiationSession:
    """创建薪资谈判会话，并生成 HR 开场白。"""
    session = NegotiationSession(
        user_id=user_id,
        scenario=scenario,
        target_salary=target,
        bottom_salary=bottom,
        context=context,
        status="active",
        messages=[],
        coaching=[],
        round_count=0,
    )
    db.add(session)
    db.commit()
    db.refresh(session)

    opening = generate_hr_message(scenario, target or "", bottom or "", context or "", [], user)
    session.current_hr_message = opening
    session.messages = [{"role": "hr", "content": opening}]
    db.commit()
    return session


def get_negotiation_session(session_id: str, db: Session, user_id: str) -> Optional[NegotiationSession]:
    return db.query(NegotiationSession).filter(
        NegotiationSession.id == session_id,
        NegotiationSession.user_id == user_id,
    ).first()


def submit_negotiation_answer(
    session_id: str,
    answer: str,
    db: Session,
    user_id: str,
    user: User,
) -> Tuple[Optional[str], Optional[str], bool]:
    """提交回答，返回 (教练反馈, 下一句 HR 消息, 是否结束)。"""
    session = get_negotiation_session(session_id, db, user_id)
    if not session:
        return None, None, False
    if session.status == "finished":
        return None, None, True

    # 记录用户回答
    messages = list(session.messages or [])
    messages.append({"role": "user", "content": answer})
    session.messages = messages

    # 教练反馈
    hr_message = session.current_hr_message or ""
    coaching = coach_answer(
        hr_message, answer,
        session.target_salary, session.bottom_salary, session.context,
        user,
    )
    coaching_list = list(session.coaching or [])
    coaching_list.append({"hr": hr_message, "answer": answer, "coaching": coaching})
    session.coaching = coaching_list

    round_count = (session.round_count or 0) + 1
    session.round_count = round_count

    # 判断结束
    if round_count >= MAX_ROUNDS:
        session.status = "finished"
        db.commit()
        return coaching, None, True

    # 生成 HR 下一句（继续压价）
    next_hr = generate_hr_message(
        session.scenario, session.target_salary or "", session.bottom_salary or "",
        session.context or "", messages, user,
    )
    messages.append({"role": "hr", "content": next_hr})
    session.messages = messages
    session.current_hr_message = next_hr
    db.commit()
    return coaching, next_hr, False


def get_negotiation_summary(session_id: str, db: Session, user_id: str, user: User) -> Optional[dict]:
    session = get_negotiation_session(session_id, db, user_id)
    if not session:
        return None
    summary = generate_negotiation_summary(session.messages or [], user)
    return {
        "summary": summary,
        "round_count": session.round_count,
        "messages": session.messages,
        "coaching": session.coaching,
        "status": session.status,
        "scenario": session.scenario,
        "target_salary": session.target_salary,
        "bottom_salary": session.bottom_salary,
    }


def get_salary_reference(
    resume_text: str,
    target_city: Optional[str],
    position_hint: Optional[str],
    db: Session,
    user: User,
) -> dict:
    """根据简历估一个合理市场价（应届生起薪参考）。

    流程：读当前生效基准数据（DB 优先）→ LLM 提取画像 → 基准表算区间 → LLM 生成解读。
    薪资数字由 `salary_benchmark` 按「学历 × 方向 × 城市」规则计算，不由 LLM 编造。
    """
    # 0. 读当前生效基准数据（DB 版本化数据优先，空则回退内置基线）
    active = benchmark_store.active_data(db)
    data = {
        "degree_base": active["degree_base"],
        "direction_coef": active["direction_coef"],
        "city_tiers": active["city_tiers"],
    }
    data_year = str(active.get("data_year") or "2026")
    version = active.get("version") or 0

    # 1. LLM 读简历，提取定薪画像（学历/学校/方向/亮点），不碰数字
    profile = extract_resume_profile(resume_text, position_hint or "", user)

    # 2. 归一化到基准表标准 key
    raw_degree = str(profile.get("degree") or "")
    raw_school = str(profile.get("school_tier") or "")
    raw_direction = str(
        profile.get("direction") or profile.get("position_raw") or position_hint or ""
    )
    city_hint = target_city or str(profile.get("target_city_hint") or "")

    degree = benchmark.resolve_degree(raw_degree)
    school_tier = benchmark.resolve_school_tier(raw_school or raw_degree)
    # 本科若识别出 985/211 学校，升级为精英本科档
    if degree == "bachelor_normal" and school_tier == "elite":
        degree = "bachelor_elite"
    direction = benchmark.resolve_direction(raw_direction)
    city_tier = benchmark.resolve_city_tier(city_hint)

    # 经验年限：社招在应届起薪基础上上浮
    try:
        work_years = float(str(profile.get("work_years") or 0))
    except (TypeError, ValueError):
        work_years = 0.0
    work_years = max(0.0, work_years)

    # 3. 查表算区间 + 谈判建议（传入当前生效数据 + 经验年限）
    low, high = benchmark.estimate_salary_range(degree, direction, city_tier, data=data, experience=work_years)
    ask, target, bottom = benchmark.suggest_target_and_bottom(degree, direction, city_tier, data=data, experience=work_years)

    # 4. 组装展示字段 + 生成解读
    degree_label = benchmark.DEGREE_LABELS[degree]
    school_label = {"elite": "985/211", "associate": "专科", "normal": "普通院校"}.get(
        school_tier, "普通院校"
    )
    direction_label = benchmark.direction_label(direction, data=data)
    city_label = (data["city_tiers"].get(city_tier) or [0, ""])[1] or benchmark.CITY_TIERS[city_tier][1]
    position = str(profile.get("position_raw") or position_hint or "")
    highlights = profile.get("highlights") if isinstance(profile.get("highlights"), list) else []
    exp_label = benchmark.experience_label(work_years)

    basis = (
        f"学历「{degree_label}」底薪区间 × 岗位方向「{direction_label}」系数 × 城市档系数"
        f" × 经验「{exp_label}」系数，参考 {data_year} 届应届生起薪市场行情"
    )
    analysis = generate_salary_analysis(
        degree_label=degree_label,
        school_label=school_label,
        direction_label=direction_label,
        position=position,
        city_label=city_label,
        experience_label=exp_label,
        highlights=highlights,
        low=low,
        high=high,
        ask=ask,
        target=target,
        bottom=bottom,
        basis=basis,
        user=user,
    )

    return {
        "salary_range": {"low": low, "high": high},
        "suggest_ask": ask,
        "suggest_target": target,
        "suggest_bottom": bottom,
        "degree": degree_label,
        "school_tier": school_label,
        "direction": direction_label,
        "position": position,
        "city": city_hint or "一线城市（默认）",
        "city_tier": city_label,
        "highlights": highlights,
        "analysis": analysis,
        "market_basis": basis,
        "sources": benchmark.DATA_SOURCES,
        "data_version": version,
        "data_year": data_year,
        "data_source": active.get("source") or "",
        "data_updated_at": active.get("updated_at"),
        "caveats": [
            "以上为税前月薪，不含年终奖、绩效、补贴、股票",
            f"数据参考 {data_year} 届应届生起薪市场行情，实际以具体公司、岗位和城市为准",
        ],
    }


def refresh_benchmark(db: Session) -> dict:
    """已停用 LLM 自动估算刷新（会编造数字污染真实数据）。

    数据更新改为「定期联网采集真实行情 → 通过 import_benchmark 导入」，
    本接口保留签名以兼容前端，但直接拒绝模型估算路径。
    """
    raise ValueError("薪资基准已改为联网采集真实行情更新，不再使用模型估算，请通过「导入」写入权威数据")


def import_benchmark(
    db: Session,
    new_data: dict,
    data_year: str,
    source: str,
    note: str,
) -> dict:
    """导入权威市场薪资数据（覆盖模型估算），写入新版本。

    `new_data` 经 `salary_benchmark.sanitize_data` 校验，不合法抛 ValueError。
    """
    benchmark_store.refresh(
        db,
        new_data,
        data_year=data_year or "最新",
        source=source or "手动导入的权威数据",
        note=note or "手动导入",
    )
    return benchmark_store.active_data(db)


def get_benchmark_info(db: Session) -> dict:
    """返回当前生效基准数据的版本信息（供前端展示数据新鲜度）。"""
    active = benchmark_store.active_data(db)
    return {
        "version": active.get("version") or 0,
        "data_year": active.get("data_year") or "2026",
        "source": active.get("source") or "",
        "note": active.get("note") or "",
        "updated_at": active.get("updated_at"),
    }
