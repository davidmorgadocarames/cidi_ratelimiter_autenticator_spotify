import pyotp
from fastapi.testclient import TestClient

PASSWORD = "supersecret"


def _register_and_login(client: TestClient, email: str = "premium@example.com", password: str = PASSWORD):
    client.post("/auth/register", json={"email": email, "password": password})
    login_response = client.post("/auth/login", data={"username": email, "password": password})
    return login_response.json()["access_token"]


def _headers(access_token: str) -> dict:
    return {"Authorization": f"Bearer {access_token}"}


def _enable_totp(client: TestClient, access_token: str, password: str = PASSWORD) -> str:
    """Configura y confirma 2FA para el usuario autenticado; devuelve el secreto."""
    setup_response = client.post(
        "/2fa/setup", json={"password": password}, headers=_headers(access_token)
    )
    secret = setup_response.json()["secret"]
    code = pyotp.TOTP(secret).now()
    client.post("/2fa/verify", json={"code": code}, headers=_headers(access_token))
    return secret


def test_activate_premium_requires_auth(client: TestClient):
    response = client.post(
        "/users/me/premium/activate", json={"password": PASSWORD, "totp_code": "123456"}
    )
    assert response.status_code == 401


def test_activate_premium_blocked_without_2fa(client: TestClient):
    access_token = _register_and_login(client, email="no2fa@example.com")
    response = client.post(
        "/users/me/premium/activate",
        json={"password": PASSWORD, "totp_code": "123456"},
        headers=_headers(access_token),
    )
    assert response.status_code == 403


def test_activate_premium_rejects_wrong_password(client: TestClient):
    access_token = _register_and_login(client, email="wrongpw2@example.com")
    secret = _enable_totp(client, access_token)
    code = pyotp.TOTP(secret).now()

    response = client.post(
        "/users/me/premium/activate",
        json={"password": "not-the-password", "totp_code": code},
        headers=_headers(access_token),
    )
    assert response.status_code == 401


def test_activate_premium_rejects_wrong_totp_code(client: TestClient):
    access_token = _register_and_login(client, email="wrongtotp@example.com")
    secret = _enable_totp(client, access_token)
    real_code = pyotp.TOTP(secret).now()
    wrong_code = "000000" if real_code != "000000" else "111111"

    response = client.post(
        "/users/me/premium/activate",
        json={"password": PASSWORD, "totp_code": wrong_code},
        headers=_headers(access_token),
    )
    assert response.status_code == 401


def test_activate_and_deactivate_premium_success(client: TestClient):
    access_token = _register_and_login(client, email="fullflow@example.com")
    secret = _enable_totp(client, access_token)
    code = pyotp.TOTP(secret).now()

    activate_response = client.post(
        "/users/me/premium/activate",
        json={"password": PASSWORD, "totp_code": code},
        headers=_headers(access_token),
    )
    assert activate_response.status_code == 200
    assert activate_response.json()["is_premium"] is True

    me_response = client.get("/auth/me", headers=_headers(access_token))
    assert me_response.json()["is_premium"] is True

    deactivate_response = client.post(
        "/users/me/premium/deactivate", headers=_headers(access_token)
    )
    assert deactivate_response.status_code == 200
    assert deactivate_response.json()["is_premium"] is False

    me_response_after = client.get("/auth/me", headers=_headers(access_token))
    assert me_response_after.json()["is_premium"] is False


def test_deactivate_premium_does_not_require_2fa(client: TestClient):
    access_token = _register_and_login(client, email="deactivatenoauth@example.com")
    response = client.post("/users/me/premium/deactivate", headers=_headers(access_token))
    assert response.status_code == 200
    assert response.json()["is_premium"] is False


def test_deactivate_premium_requires_auth(client: TestClient):
    response = client.post("/users/me/premium/deactivate")
    assert response.status_code == 401
