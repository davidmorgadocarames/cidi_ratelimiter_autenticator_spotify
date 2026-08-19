import threading
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient
from httpx import Response
from sqlalchemy.orm import Session

from app.main import app
from app.models.user import RefreshToken


def _register(
    client: TestClient, email: str, password: str = "supersecret"
) -> Response:
    return client.post("/auth/register", json={"email": email, "password": password})


def _register_and_login(
    client: TestClient, email: str = "login@example.com", password: str = "supersecret"
) -> Response:
    _register(client, email, password)
    return client.post("/auth/login", data={"username": email, "password": password})


def test_register_success(client: TestClient) -> None:
    response = _register(client, "user@example.com")
    assert response.status_code == 201
    body = response.json()
    assert body["email"] == "user@example.com"
    assert "hashed_password" not in body


def test_register_duplicate_email_rejected(client: TestClient) -> None:
    _register(client, "dup@example.com")
    response = _register(client, "dup@example.com")
    assert response.status_code == 409


def test_login_success_returns_access_token_and_refresh_cookie(
    client: TestClient,
) -> None:
    response = _register_and_login(client)
    assert response.status_code == 200
    body = response.json()
    assert "access_token" in body
    assert body["token_type"] == "bearer"
    assert "refresh_token" in response.cookies


def test_login_wrong_password_rejected(client: TestClient) -> None:
    _register(client, "wrong@example.com")
    response = client.post(
        "/auth/login",
        data={"username": "wrong@example.com", "password": "bad-password"},
    )
    assert response.status_code == 401


def test_login_nonexistent_user_rejected(client: TestClient) -> None:
    response = client.post(
        "/auth/login", data={"username": "ghost@example.com", "password": "whatever"}
    )
    assert response.status_code == 401


def test_me_returns_current_user_with_valid_access_token(client: TestClient) -> None:
    login_response = _register_and_login(client, email="me@example.com")
    access_token = login_response.json()["access_token"]
    response = client.get(
        "/auth/me", headers={"Authorization": f"Bearer {access_token}"}
    )
    assert response.status_code == 200
    assert response.json()["email"] == "me@example.com"


def test_me_rejects_missing_token(client: TestClient) -> None:
    response = client.get("/auth/me")
    assert response.status_code == 401


def test_refresh_rotates_token(client: TestClient) -> None:
    login_response = _register_and_login(client, email="refresh@example.com")
    old_cookie = login_response.cookies.get("refresh_token")

    refresh_response = client.post("/auth/refresh")
    assert refresh_response.status_code == 200
    assert "access_token" in refresh_response.json()

    new_cookie = refresh_response.cookies.get("refresh_token")
    assert new_cookie is not None
    assert new_cookie != old_cookie


def test_refresh_without_cookie_rejected(client: TestClient) -> None:
    response = client.post("/auth/refresh")
    assert response.status_code == 401


def test_refresh_reuse_of_rotated_token_revokes_whole_chain(client: TestClient) -> None:
    login_response = _register_and_login(client, email="reuse@example.com")
    old_cookie = login_response.cookies.get("refresh_token")
    assert old_cookie is not None

    # Primer refresh: rota el token, el viejo queda revocado. El cliente ya trae
    # el nuevo cookie guardado en su jar tras esta llamada.
    first_refresh = client.post("/auth/refresh")
    assert first_refresh.status_code == 200
    new_cookie = first_refresh.cookies.get("refresh_token")
    assert new_cookie is not None

    # Reusar el token viejo (ya revocado) debe fallar...
    client.cookies.set("refresh_token", old_cookie)
    reuse_response = client.post("/auth/refresh")
    assert reuse_response.status_code == 401

    # ...y debe haber revocado también el token nuevo (toda la family_id).
    client.cookies.set("refresh_token", new_cookie)
    should_fail = client.post("/auth/refresh")
    assert should_fail.status_code == 401


def test_logout_revokes_refresh_token(client: TestClient) -> None:
    _register_and_login(client, email="logout@example.com")

    logout_response = client.post("/auth/logout")
    assert logout_response.status_code == 204

    refresh_response = client.post("/auth/refresh")
    assert refresh_response.status_code == 401


def test_refresh_with_expired_token_rejected(
    client: TestClient, db_session: Session
) -> None:
    login_response = _register_and_login(client, email="expired@example.com")
    assert login_response.cookies.get("refresh_token") is not None

    # Forzamos la expiración directamente en DB: esperar 7 días reales no es viable
    # en un test, y este registro no está revocado (revoked_at sigue null), así que
    # ejercita la rama de "expirado" y no la de "reuso detectado".
    record = db_session.query(RefreshToken).one()
    record.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    db_session.commit()

    response = client.post("/auth/refresh")
    assert response.status_code == 401
    assert response.json()["detail"] == "Refresh token expirado"


def test_refresh_concurrent_requests_only_one_succeeds(client: TestClient) -> None:
    """Dos requests simultáneos con el MISMO refresh token (doble pestaña, retry de
    red) no deben poder generar dos tokens hijos válidos para la misma family_id.
    Verifica el fix de la race condition (SELECT ... FOR UPDATE en app/api/auth.py)."""
    login_response = _register_and_login(client, email="concurrent@example.com")
    cookie_value = login_response.cookies.get("refresh_token")
    assert cookie_value is not None

    results: list[int] = []
    barrier = threading.Barrier(2)

    def _do_refresh() -> None:
        local_client = TestClient(app)
        local_client.cookies.set("refresh_token", cookie_value)
        barrier.wait(timeout=5)
        response = local_client.post("/auth/refresh")
        results.append(response.status_code)

    threads = [threading.Thread(target=_do_refresh) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert sorted(results) == [200, 401]
