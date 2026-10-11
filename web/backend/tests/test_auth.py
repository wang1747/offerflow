"""认证接口测试。

契约（与 app/schemas/auth.py 保持一致）：
    POST /register  {account(邮箱，唯一登录标识), name(可选称呼), password}  → 200
    POST /login     {username(登录邮箱), password}                          → 200
    GET  /me        需要 Bearer Token                                       → 200
"""

from io import BytesIO

from PIL import Image


def test_register_login_and_me(auth_client):
    register = auth_client.post(
        "/api/v1/auth/register",
        json={
            "account": "alice@example.com",
            "name": "alice",
            "password": "Pass123456",
        },
    )
    assert register.status_code == 200
    assert register.json()["data"]["name"] == "alice"

    login = auth_client.post(
        "/api/v1/auth/login",
        json={"username": "alice@example.com", "password": "Pass123456"},
    )
    assert login.status_code == 200
    token = login.json()["data"]["access_token"]
    assert token

    me = auth_client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert me.status_code == 200
    assert me.json()["data"]["name"] == "alice"


def test_register_uses_email_prefix_as_default_name(auth_client):
    """不填 name 时，按邮箱前缀自动生成显示名。"""
    register = auth_client.post(
        "/api/v1/auth/register",
        json={"account": "carol@example.com", "password": "Pass123456"},
    )
    assert register.status_code == 200
    assert register.json()["data"]["name"] == "carol"


def test_register_rejects_invalid_account(auth_client):
    """非邮箱格式的账号应被拒（400）。"""
    register = auth_client.post(
        "/api/v1/auth/register",
        json={"account": "not-an-email", "password": "Pass123456"},
    )
    assert register.status_code == 400


def test_duplicate_register_rejected(auth_client):
    payload = {"account": "bob@example.com", "name": "bob", "password": "Pass123456"}
    assert auth_client.post("/api/v1/auth/register", json=payload).status_code == 200
    assert auth_client.post("/api/v1/auth/register", json=payload).status_code == 409


def test_invalid_login_rejected(auth_client):
    response = auth_client.post(
        "/api/v1/auth/login",
        json={"username": "missing@example.com", "password": "Pass123456"},
    )
    assert response.status_code == 401


def test_oversized_avatar_is_compressed_instead_of_rejected(
    auth_client, tmp_path, monkeypatch,
):
    import app.modules.auth.routes as auth_routes

    auth_client.post(
        "/api/v1/auth/register",
        json={
            "account": "avatar@example.com",
            "name": "avatar",
            "password": "Pass123456",
        },
    )
    login = auth_client.post(
        "/api/v1/auth/login",
        json={"username": "avatar@example.com", "password": "Pass123456"},
    )
    token = login.json()["data"]["access_token"]

    image = Image.effect_noise((3200, 3200), 100).convert("RGB")
    buffer = BytesIO()
    image.save(buffer, format="JPEG", quality=100)
    source = buffer.getvalue()
    assert len(source) > 2 * 1024 * 1024

    avatar_dir = tmp_path / "avatars"
    monkeypatch.setattr(auth_routes, "AVATAR_DIR", str(avatar_dir))
    response = auth_client.post(
        "/api/v1/auth/avatar",
        files={"file": ("portrait.jpg", source, "image/jpeg")},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200, response.text
    filename = response.json()["data"]["avatar"].rsplit("/", 1)[-1]
    stored = avatar_dir / filename
    assert stored.is_file()
    assert stored.stat().st_size <= 2 * 1024 * 1024
    with Image.open(stored) as compressed:
        assert max(compressed.size) <= 2048
