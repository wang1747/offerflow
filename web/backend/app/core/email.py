"""邮件发送工具（找回密码验证码）。SMTP 未配置时抛 RuntimeError，调用方应友好提示。"""

import smtplib
from email.header import Header
from email.mime.text import MIMEText
from email.utils import formataddr

from ..config import get_settings


def send_email(to: str, subject: str, body: str) -> None:
    """发送纯文本邮件。SMTP 未配置时抛 RuntimeError。"""
    settings = get_settings()
    if not (settings.smtp_host and settings.smtp_user and settings.smtp_password):
        raise RuntimeError("邮件服务未配置")

    from_addr = settings.smtp_from or settings.smtp_user

    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = Header(subject, "utf-8")
    # From 头的邮箱地址必须保持 ASCII，不能整体编码（否则 QQ 等会报 550 From invalid）
    msg["From"] = formataddr((str(Header("OfferFlow", "utf-8")), from_addr))
    msg["To"] = to

    if settings.smtp_use_ssl:
        server = smtplib.SMTP_SSL(settings.smtp_host, settings.smtp_port, timeout=15)
    else:
        server = smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=15)
        server.starttls()
    try:
        server.login(settings.smtp_user, settings.smtp_password)
        server.sendmail(from_addr, [to], msg.as_string())
    finally:
        try:
            server.quit()
        except Exception:
            pass
