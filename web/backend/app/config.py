from functools import lru_cache
from pathlib import Path
from typing import Optional
import warnings

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DB_PATH = PROJECT_ROOT / "offerflow.db"


class Settings(BaseSettings):
    # ===== 应用环境 =====
    app_env: str = Field(
        default="development",
        description="运行环境: development | staging | production",
        pattern=r"^(development|staging|production)$"
    )

    # ===== LLM 配置 =====
    deepseek_api_key: Optional[SecretStr] = Field(
        default=None,
        description="DeepSeek API Key（用户自带 Key 后不再需要服务器默认 Key）"
    )
    deepseek_base_url: str = "https://api.deepseek.com"
    llm_model: str = "deepseek-flash"

    # ===== 数据库 =====
    database_url: str = f"sqlite:///{DB_PATH.as_posix()}"

    # ===== 向量库 =====
    chroma_persist_path: str = "./chroma_db"

    # ===== 默认用户 =====
    default_user_id: str = "default"
    default_user_name: str = "demo"
    default_user_password: str = "demo1234"

    # ===== CORS 配置 =====
    cors_origins: str = Field(
        default="http://localhost:5173,http://127.0.0.1:5173,http://localhost,http://127.0.0.1",
        description="允许的跨域来源，逗号分隔"
    )

    # ===== JWT 配置 =====
    jwt_secret_key: SecretStr = Field(
        default=SecretStr("offerflow-secret-key-change-in-production"),
        description="JWT 签名密钥，生产环境必须替换为至少32位的随机字符串"
    )
    jwt_algorithm: str = Field(default="HS256", description="JWT 签名算法，仅支持 HS256")
    jwt_access_token_expire_minutes: int = Field(
        default=60 * 24 * 7,
        description="访问令牌有效期（分钟），默认 7 天"
    )

    # ===== 加密配置（BYOK） =====
    encryption_key: Optional[str] = Field(
        default=None,
        description="加密密钥（用于 API Key 加密存储），生产环境必须配置 32 位以上字符串"
    )

    # ===== 提醒推送 =====
    reminder_webhook_url: Optional[str] = Field(
        default=None,
        description="提醒推送 Webhook 地址，例如企业微信/钉钉/n8n"
    )
    reminder_check_interval_minutes: int = Field(
        default=60,
        description="提醒检查间隔（分钟）"
    )
    reminder_service_token: str = Field(
        default="change-this-reminder-token",
        description="n8n 调用提醒导出接口时的服务令牌"
    )

    # ===== 薪资基准数据刷新 =====
    salary_benchmark_refresh_days: int = Field(
        default=30,
        description="薪资基准数据自动刷新周期（天），0 表示禁用自动刷新"
    )

    # ===== 简历生成并发上限 =====
    resume_gen_max_concurrency: int = Field(
        default=2,
        description=(
            "并发简历生成（含逐段重写/候选生成）上限。LLM 走远端 API、非本地模型，"
            "主要约束是平台 50k token/日配额与事件循环而非内存；1.6GB 小机建议 1~2。"
            "可用环境变量 RESUME_GEN_MAX_CONCURRENCY 覆盖。"
        )
    )

    # ===== 邮件配置（找回密码验证码）=====
    smtp_host: Optional[str] = Field(default=None, description="SMTP 服务器，如 smtp.qq.com")
    smtp_port: int = Field(default=465, description="SMTP 端口（465=SSL，587=STARTTLS）")
    smtp_user: Optional[str] = Field(default=None, description="发件邮箱账号")
    smtp_password: Optional[str] = Field(default=None, description="SMTP 授权码（非登录密码）")
    smtp_from: Optional[str] = Field(default=None, description="发件人显示，默认同 smtp_user")
    smtp_use_ssl: bool = Field(default=True, description="是否用 SSL（465 端口为 True，587 为 False）")

    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
        validate_default=True,
    )

    # ===== 字段校验器 =====

    @field_validator("jwt_secret_key")
    def check_secret_length(cls, v: SecretStr):
        """校验 JWT 密钥长度（HS256 建议至少 32 字符）"""
        secret = v.get_secret_value()
        if len(secret) < 32:
            raise ValueError("JWT_SECRET_KEY 长度不能小于32位，请使用随机生成的长字符串")
        return v

    @field_validator("jwt_algorithm")
    def valid_algorithm(cls, v: str):
        """限制仅允许 HS256"""
        if v not in ("HS256",):
            raise ValueError("仅支持 HS256 签名算法")
        return v

    @field_validator("encryption_key")
    def validate_encryption_key(cls, v: Optional[str]):
        """校验加密密钥长度"""
        if v is not None and len(v) < 32:
            raise ValueError("ENCRYPTION_KEY 长度不能小于32位")
        return v

    @model_validator(mode="after")
    def _validate_jwt_secret_env(self):
        """校验 JWT 密钥：开发环境警告，生产环境直接报错"""
        default_secret = "offerflow-secret-key-change-in-production"
        current_secret = self.jwt_secret_key.get_secret_value()

        if current_secret == default_secret:
            if self.app_env == "development":
                warnings.warn(
                    "⚠️ 开发环境使用默认 JWT 密钥，上线前务必替换为随机生成的 32+ 位密钥！",
                    RuntimeWarning
                )
            else:
                raise ValueError(
                    f"生产环境禁止使用默认 JWT_SECRET_KEY（当前环境: {self.app_env}），"
                    "请通过环境变量设置随机生成的 32+ 位密钥"
                )
        return self

    @model_validator(mode="after")
    def _resolve_project_paths(self):
        if self.database_url.startswith("sqlite:///./"):
            relative = self.database_url[len("sqlite:///./"):]
            self.database_url = f"sqlite:///{(PROJECT_ROOT / relative).as_posix()}"
        if not Path(self.chroma_persist_path).is_absolute():
            self.chroma_persist_path = str(PROJECT_ROOT / self.chroma_persist_path)
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
