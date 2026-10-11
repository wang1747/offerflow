import os
import random
import re
import threading
import time
import uuid
from typing import Optional

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse
from fastapi.security import (
    HTTPAuthorizationCredentials,
    HTTPBearer,
    OAuth2PasswordRequestForm,
)
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.email import send_email
from app.core.image_utils import compress_image
from app.core.security import (
    create_access_token,
    decode_access_token,
    hash_password,
    verify_password,
)
from app.models.user import User
from app.schemas.auth import (
    ChangeEmailRequest,
    ChangeEmailSendCodeRequest,
    ChangePasswordRequest,
    LoginRequest,
    LoginResponse,
    ProfileUpdateRequest,
    RegisterRequest,
    RegisterResponse,
    ResetPasswordRequest,
    SendCodeRequest,
    UserResponse,
)
from app.schemas.common import ApiResponse, success_response

router = APIRouter(prefix="/api/v1/auth", tags=["Auth"])
bearer_scheme = HTTPBearer(auto_error=False)
_login_attempts: dict[str, list[float]] = {}
_login_attempts_lock = threading.Lock()

# 验证码存储：key = "account:email" 或 "change-email:{user_id}:{email}"
# -> (code, expire_ts, last_send_ts)
_verification_codes: dict[str, tuple[str, float, float]] = {}
_verification_codes_lock = threading.Lock()

# IP 级发码限流：IP -> 发码时间戳列表（堵住换邮箱刷验证码/SMTP 轰炸）
_ip_send_log: dict[str, list[float]] = {}
_ip_send_log_lock = threading.Lock()

_CODE_TTL = 600          # 验证码有效期 10 分钟
_CODE_RESEND_INTERVAL = 60  # 同账号重发间隔 60 秒

_IP_WINDOW_SECONDS = 60    # IP 短窗口 60 秒
_IP_WINDOW_MAX = 5         # 短窗口内最多发 5 次
_IP_DAY_SECONDS = 86400    # IP 日窗口 24 小时
_IP_DAY_MAX = 20           # 每日最多发 20 次

# 账号格式校验：邮箱
_EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")


def _classify_account(account: str):
    """识别账号类型，返回 (kind, value, display_name)。

    kind ∈ {"email", ""}；"" 表示格式不合法。
    display_name 用于填充用户显示名（邮箱取 @ 前部分）。
    """
    value = account.strip()
    if _EMAIL_RE.match(value):
        return "email", value, value.split("@", 1)[0]
    return "", "", ""


def _check_login_rate_limit(username: str) -> None:
    now = time.time()
    window = 60
    max_attempts = 5
    with _login_attempts_lock:
        attempts = [ts for ts in _login_attempts.get(username, []) if now - ts < window]
        if len(attempts) >= max_attempts:
            raise HTTPException(status_code=429, detail="登录尝试过于频繁，请稍后再试")
        attempts.append(now)
        _login_attempts[username] = attempts


def _get_client_ip(request: Request) -> str:
    """解析客户端真实 IP：优先 X-Forwarded-For 首个，其次 X-Real-IP，兜底直连。"""
    xff = request.headers.get("x-forwarded-for")
    if xff:
        return xff.split(",")[0].strip()
    xri = request.headers.get("x-real-ip")
    if xri:
        return xri.strip()
    return request.client.host if request.client else "unknown"


def _check_ip_rate_limit(ip: str) -> None:
    """按 IP 限制验证码发送：60 秒窗口 + 每日总量，堵住换邮箱刷验证码。"""
    now = time.time()
    with _ip_send_log_lock:
        times = [t for t in _ip_send_log.get(ip, []) if now - t < _IP_DAY_SECONDS]
        recent = [t for t in times if now - t < _IP_WINDOW_SECONDS]
        if len(recent) >= _IP_WINDOW_MAX:
            raise HTTPException(status_code=429, detail="发送过于频繁，请稍后再试")
        if len(times) >= _IP_DAY_MAX:
            raise HTTPException(status_code=429, detail="今日验证码发送次数已达上限，请明天再试")
        times.append(now)
        _ip_send_log[ip] = times


def _issue_code(key: str, target_email: str, subject: str, body_tpl: str) -> None:
    """生成验证码并通过 SMTP 发送，成功后写入存储（含重发间隔限制）。"""
    now = time.time()
    with _verification_codes_lock:
        existing = _verification_codes.get(key)
        if existing and now - existing[2] < _CODE_RESEND_INTERVAL:
            raise HTTPException(status_code=429, detail="发送过于频繁，请稍后再试")

    code = f"{random.randint(0, 999999):06d}"
    expire = now + _CODE_TTL

    try:
        send_email(target_email, subject, body_tpl.format(code=code))
    except RuntimeError:
        raise HTTPException(status_code=503, detail="邮件服务未配置，请联系管理员")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"验证码发送失败：{e}")

    with _verification_codes_lock:
        _verification_codes[key] = (code, expire, now)


def _consume_code(key: str, code: str) -> None:
    """校验并一次性消费验证码，失败抛 HTTPException。"""
    code = (code or "").strip()
    with _verification_codes_lock:
        stored = _verification_codes.get(key)
        if not stored:
            raise HTTPException(status_code=400, detail="请先获取验证码")
        stored_code, expire, _ = stored
        if time.time() > expire:
            _verification_codes.pop(key, None)
            raise HTTPException(status_code=400, detail="验证码已过期，请重新获取")
        if stored_code != code:
            raise HTTPException(status_code=400, detail="验证码错误")
        _verification_codes.pop(key, None)  # 一次性使用


def get_current_user_required(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(bearer_scheme),
    db: Session = Depends(get_db),
) -> User:
    user = _resolve_token_user(credentials, db)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return user


def get_current_user_optional(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(bearer_scheme),
    db: Session = Depends(get_db),
) -> Optional[User]:
    return _resolve_token_user(credentials, db)


def _resolve_token_user(
    credentials: Optional[HTTPAuthorizationCredentials],
    db: Session,
) -> Optional[User]:
    if credentials is None:
        return None
    user_id = decode_access_token(credentials.credentials)
    if not user_id:
        return None
    return db.query(User).filter(User.id == user_id, User.is_active.is_(True)).first()


def _authenticate_user(db: Session, account: str, password: str) -> Optional[User]:
    """按账号登录：仅支持邮箱（邮箱即唯一身份）。"""
    email = (account or "").strip()
    if not _EMAIL_RE.match(email):
        return None
    user = db.query(User).filter(User.email == email).first()
    if not user or not user.is_active or not user.hashed_password or not verify_password(password, user.hashed_password):
        return None
    return user


def _is_account_disabled(db: Session, account: str) -> bool:
    """账号存在但被禁用时返回 True，用于给用户更明确的登录失败提示。"""
    email = (account or "").strip()
    if not _EMAIL_RE.match(email):
        return False
    user = db.query(User).filter(User.email == email).first()
    return bool(user and not user.is_active)


@router.post("/register", response_model=ApiResponse[RegisterResponse])
def register(req: RegisterRequest, db: Session = Depends(get_db)):
    # 是否开放注册由管理员在后台控制
    from app.modules.admin.services import get_setting_value

    if not get_setting_value(db, "allow_register", True):
        raise HTTPException(status_code=403, detail="当前未开放注册，请联系管理员")

    account = req.account.strip()
    kind, value, auto_display = _classify_account(account)
    if not kind:
        raise HTTPException(status_code=400, detail="请输入正确的邮箱")

    # 用户称呼：填了用填的，没填则按账号自动生成（邮箱前缀）
    display_name = (req.name or "").strip()
    if not display_name:
        display_name = auto_display

    # 邮箱唯一性校验，避免重复注册
    existing = db.query(User).filter(User.email == value).first()
    if existing:
        raise HTTPException(status_code=409, detail="该邮箱已被注册")

    # allow_corpus 显式置 False：隐私默认关闭。不依赖模型默认值，
    # 避免将来有人改动 models/user.py 的 default 时又静默变成「默认共享」。
    user = User(name=display_name, email=value, hashed_password=hash_password(req.password),
                allow_corpus=False)
    db.add(user)
    db.commit()
    db.refresh(user)
    return success_response(RegisterResponse.model_validate(user))


@router.post("/login", response_model=ApiResponse[LoginResponse])
def login(req: LoginRequest, db: Session = Depends(get_db)):
    _check_login_rate_limit(req.username)
    user = _authenticate_user(db, req.username, req.password)
    if not user:
        if _is_account_disabled(db, req.username):
            raise HTTPException(status_code=403, detail="账号已被禁用")
        raise HTTPException(status_code=401, detail="邮箱或密码错误")

    token = create_access_token(user.id)
    return success_response(
        LoginResponse(
            access_token=token,
            token_type="bearer",
            user=UserResponse.model_validate(user),
        )
    )


@router.post("/token", response_model=ApiResponse[LoginResponse])
def login_form(
    form: OAuth2PasswordRequestForm = Depends(),
    db: Session = Depends(get_db),
):
    _check_login_rate_limit(form.username)
    user = _authenticate_user(db, form.username, form.password)
    if not user:
        if _is_account_disabled(db, form.username):
            raise HTTPException(status_code=403, detail="账号已被禁用")
        raise HTTPException(status_code=401, detail="邮箱或密码错误")

    token = create_access_token(user.id)
    return success_response(
        LoginResponse(
            access_token=token,
            token_type="bearer",
            user=UserResponse.model_validate(user),
        )
    )


@router.get("/me", response_model=ApiResponse[UserResponse])
def me(current_user: User = Depends(get_current_user_required)):
    return success_response(UserResponse.model_validate(current_user))


@router.patch("/profile", response_model=ApiResponse[UserResponse])
def update_profile(
    req: ProfileUpdateRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_required),
):
    """更新个人资料（昵称）。"""
    current_user.name = req.name.strip()
    db.commit()
    db.refresh(current_user)
    return success_response(UserResponse.model_validate(current_user))


# 头像存储目录（挂载 upload-data volume，与社区附件同盘）
AVATAR_DIR = os.environ.get("AVATAR_UPLOAD_DIR", "/app/uploads/avatar")
AVATAR_EXT_RE = re.compile(r"^[0-9a-f]{32}\.(jpg|jpeg|png|gif|webp)$")
AVATAR_ALLOWED_EXT = {".jpg", ".jpeg", ".png", ".gif", ".webp"}
AVATAR_MAX_SIZE = 2 * 1024 * 1024  # 2MB
AVATAR_INPUT_MAX_SIZE = 20 * 1024 * 1024


@router.post("/avatar", response_model=ApiResponse[UserResponse])
async def upload_avatar(
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_required),
):
    """上传/更新头像。存盘后更新 user.avatar，返回最新用户信息。"""
    ext = os.path.splitext(file.filename or "")[1].lower()
    if ext not in AVATAR_ALLOWED_EXT:
        raise HTTPException(status_code=400, detail="仅支持 jpg/png/gif/webp 图片")
    content = await file.read()
    if len(content) > AVATAR_INPUT_MAX_SIZE:
        raise HTTPException(status_code=413, detail="原始图片不能超过 20MB")
    try:
        content, _, stored_ext = compress_image(content, max_bytes=AVATAR_MAX_SIZE)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    os.makedirs(AVATAR_DIR, exist_ok=True)
    stored = f"{uuid.uuid4().hex}{stored_ext}"
    with open(os.path.join(AVATAR_DIR, stored), "wb") as fh:
        fh.write(content)

    # 清理旧头像文件
    old = current_user.avatar
    if old:
        old_name = old.rsplit("/", 1)[-1]
        old_path = os.path.join(AVATAR_DIR, old_name)
        if AVATAR_EXT_RE.match(old_name) and os.path.isfile(old_path):
            try:
                os.remove(old_path)
            except OSError:
                pass

    current_user.avatar = f"/api/v1/auth/avatar/{stored}"
    db.commit()
    db.refresh(current_user)
    return success_response(UserResponse.model_validate(current_user))


@router.get("/avatar/{name}")
async def get_avatar(name: str):
    """头像文件访问（匿名可读，<img> 带不上 Authorization）。文件名白名单防路径穿越。"""
    if not AVATAR_EXT_RE.match(name.lower()):
        raise HTTPException(status_code=404, detail="头像不存在")
    path = os.path.join(AVATAR_DIR, name.lower())
    if not os.path.isfile(path):
        raise HTTPException(status_code=404, detail="头像不存在")
    return FileResponse(path)


@router.post("/change-email/send-code", response_model=ApiResponse[dict])
def send_change_email_code(
    req: ChangeEmailSendCodeRequest,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_required),
):
    """换绑邮箱第一步：向新邮箱发送验证码。"""
    _check_ip_rate_limit(_get_client_ip(request))
    new_email = (req.new_email or "").strip().lower()
    if not _EMAIL_RE.match(new_email):
        raise HTTPException(status_code=400, detail="请输入正确的邮箱")
    if (current_user.email or "").strip().lower() == new_email:
        raise HTTPException(status_code=400, detail="新邮箱与当前一致")

    existing = db.query(User).filter(User.email == new_email).first()
    if existing and existing.id != current_user.id:
        raise HTTPException(status_code=409, detail="该邮箱已被其他账号使用")

    key = f"change-email:{current_user.id}:{new_email}"
    _issue_code(
        key,
        new_email,
        "OfferFlow 换绑邮箱验证码",
        "您的换绑邮箱验证码是 {code}，10 分钟内有效。如非本人操作请忽略本邮件。",
    )
    return success_response({"message": "验证码已发送", "email_masked": _mask_email(new_email)})


@router.post("/change-email", response_model=ApiResponse[UserResponse])
def change_email(
    req: ChangeEmailRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_required),
):
    """换绑邮箱第二步：校验新邮箱验证码后完成换绑。"""
    new_email = (req.new_email or "").strip().lower()
    if not _EMAIL_RE.match(new_email):
        raise HTTPException(status_code=400, detail="请输入正确的邮箱")
    if (current_user.email or "").strip().lower() == new_email:
        raise HTTPException(status_code=400, detail="新邮箱与当前一致")

    existing = db.query(User).filter(User.email == new_email).first()
    if existing and existing.id != current_user.id:
        raise HTTPException(status_code=409, detail="该邮箱已被其他账号使用")

    key = f"change-email:{current_user.id}:{new_email}"
    _consume_code(key, req.code)

    current_user.email = new_email
    db.commit()
    db.refresh(current_user)
    return success_response(UserResponse.model_validate(current_user))


@router.post("/logout")
def logout(current_user: User = Depends(get_current_user_required)):
    return {"success": True, "data": None, "error": None}


@router.post("/change-password", response_model=ApiResponse[dict])
def change_password(
    req: ChangePasswordRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_required),
):
    """登录用户修改自己的密码：校验旧密码，新密码 bcrypt 加密存储。"""
    if not current_user.hashed_password or not verify_password(req.old_password, current_user.hashed_password):
        raise HTTPException(status_code=400, detail="当前密码不正确")

    if req.new_password == req.old_password:
        raise HTTPException(status_code=400, detail="新密码不能与当前密码相同")

    current_user.hashed_password = hash_password(req.new_password)
    db.commit()
    return success_response({"message": "密码已修改，请使用新密码重新登录"})


def _mask_email(email: str) -> str:
    """邮箱脱敏：zhan***@qq.com，用于前端提示发到了哪个邮箱。"""
    local, _, domain = email.partition("@")
    if len(local) <= 2:
        head = local[:1]
    else:
        head = local[:2]
    return f"{head}***@{domain}"


def _resolve_account_and_email(db: Session, account: str, email: Optional[str]):
    """定位账号与收件邮箱（账号即邮箱）。

    返回 (user, target_email)。账号不存在返回 (None, None)。
    """
    kind, value, _ = _classify_account(account)
    if not kind:
        raise HTTPException(status_code=400, detail="请输入正确的邮箱")

    user = db.query(User).filter(User.email == value).first()
    return user, value


@router.post("/forgot-password/send-code", response_model=ApiResponse[dict])
def send_verification_code(req: SendCodeRequest, request: Request, db: Session = Depends(get_db)):
    _check_ip_rate_limit(_get_client_ip(request))
    account = req.account.strip()
    user, target_email = _resolve_account_and_email(db, account, req.email)
    if not user:
        raise HTTPException(status_code=404, detail="该账号未注册")

    key = f"{account}:{target_email}"
    now = time.time()
    with _verification_codes_lock:
        existing = _verification_codes.get(key)
        if existing and now - existing[2] < _CODE_RESEND_INTERVAL:
            raise HTTPException(status_code=429, detail="发送过于频繁，请稍后再试")

    code = f"{random.randint(0, 999999):06d}"
    expire = now + _CODE_TTL

    try:
        send_email(
            target_email,
            "OfferFlow 找回密码验证码",
            f"您的验证码是 {code}，10 分钟内有效。如非本人操作请忽略本邮件。",
        )
    except RuntimeError:
        raise HTTPException(status_code=503, detail="邮件服务未配置，请联系管理员")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"验证码发送失败：{e}")

    with _verification_codes_lock:
        _verification_codes[key] = (code, expire, now)

    return success_response({"message": "验证码已发送", "email_masked": _mask_email(target_email)})


@router.post("/forgot-password/reset", response_model=ApiResponse[dict])
def reset_password(req: ResetPasswordRequest, db: Session = Depends(get_db)):
    account = req.account.strip()
    user, target_email = _resolve_account_and_email(db, account, req.email)
    if not user:
        raise HTTPException(status_code=404, detail="该账号未注册")

    key = f"{account}:{target_email}"
    code = req.code.strip()
    with _verification_codes_lock:
        stored = _verification_codes.get(key)
        if not stored:
            raise HTTPException(status_code=400, detail="请先获取验证码")
        stored_code, expire, _ = stored
        if time.time() > expire:
            _verification_codes.pop(key, None)
            raise HTTPException(status_code=400, detail="验证码已过期，请重新获取")
        if stored_code != code:
            raise HTTPException(status_code=400, detail="验证码错误")
        _verification_codes.pop(key, None)  # 一次性使用

    user.hashed_password = hash_password(req.new_password)
    db.commit()
    return success_response({"message": "密码已重置，请使用新密码登录"})
