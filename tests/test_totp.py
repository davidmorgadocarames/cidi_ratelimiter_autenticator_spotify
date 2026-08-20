import threading
from datetime import datetime, timedelta, timezone

import pyotp
from fastapi.testclient import TestClient
from httpx import Response
from sqlalchemy.orm import Session

from app.main import app
from app.models.user import User

PASSWORD = "supersecret"


def _register_and_login(
    client: TestClient, email: str = "totp@example.com", password: str = PASSWORD
) -> str:
    client.post("/auth/register", json={"email": email, "password": password})
    login_response = client.post(
        "/auth/login", data={"username": email, "password": password}
    )
    access_token: str = login_response.json()["access_token"]
    return access_token


def _headers(access_token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {access_token}"}


def _setup(client: TestClient, access_token: str, password: str = PASSWORD) -> Response:
    return client.post(
        "/2fa/setup", json={"password": password}, headers=_headers(access_token)
    )


def test_setup_requires_auth(client: TestClient) -> None:
    response = client.post("/2fa/setup", json={"password": PASSWORD})
    assert response.status_code == 401


def test_setup_rejects_wrong_password(client: TestClient) -> None:
    access_token = _register_and_login(client, email="wrongpw@example.com")
    response = _setup(client, access_token, password="not-the-password")
    assert response.status_code == 401


def test_setup_returns_secret_and_qr(client: TestClient) -> None:
    access_token = _register_and_login(client, email="setup@example.com")
    response = _setup(client, access_token)
    assert response.status_code == 200
    body = response.json()
    assert len(body["secret"]) >= 16
    assert body["otpauth_uri"].startswith("otpauth://totp/")
    assert len(body["qr_code_base64"]) > 100


def test_setup_blocked_if_already_confirmed(
    client: TestClient, db_session: Session
) -> None:
    access_token = _register_and_login(client, email="doublesetup@example.com")
    secret = _setup(client, access_token).json()["secret"]

    code = pyotp.TOTP(secret).now()
    verify_response = client.post(
        "/2fa/verify", json={"code": code}, headers=_headers(access_token)
    )
    assert verify_response.json() == {"enabled": True, "valid": False}

    second_setup = _setup(client, access_token)
    assert second_setup.status_code == 409


def test_verify_without_setup_returns_400(client: TestClient) -> None:
    access_token = _register_and_login(client, email="noverify@example.com")
    response = client.post(
        "/2fa/verify", json={"code": "123456"}, headers=_headers(access_token)
    )
    assert response.status_code == 400


def test_verify_with_valid_code_confirms_setup(
    client: TestClient, db_session: Session
) -> None:
    access_token = _register_and_login(client, email="confirm@example.com")
    secret = _setup(client, access_token).json()["secret"]

    code = pyotp.TOTP(secret).now()
    response = client.post(
        "/2fa/verify", json={"code": code}, headers=_headers(access_token)
    )
    assert response.status_code == 200
    assert response.json() == {"enabled": True, "valid": False}

    db_user = db_session.query(User).filter_by(email="confirm@example.com").one()
    assert db_user.totp_confirmed_at is not None
    assert db_user.totp_enabled is True


def test_verify_when_already_confirmed_just_validates(client: TestClient) -> None:
    access_token = _register_and_login(client, email="already@example.com")
    secret = _setup(client, access_token).json()["secret"]
    totp = pyotp.TOTP(secret)
    client.post(
        "/2fa/verify", json={"code": totp.now()}, headers=_headers(access_token)
    )

    # Segunda verificación tras ya estar confirmado: valida sin volver a "activar".
    later_code = totp.at(datetime.now() + timedelta(seconds=30))
    response = client.post(
        "/2fa/verify", json={"code": later_code}, headers=_headers(access_token)
    )
    assert response.status_code == 200
    assert response.json() == {"enabled": False, "valid": True}


def test_verify_with_incorrect_code_rejected(client: TestClient) -> None:
    access_token = _register_and_login(client, email="badcode@example.com")
    secret = _setup(client, access_token).json()["secret"]

    real_code = pyotp.TOTP(secret).now()
    wrong_code = "000000" if real_code != "000000" else "111111"
    response = client.post(
        "/2fa/verify", json={"code": wrong_code}, headers=_headers(access_token)
    )
    assert response.status_code == 401


def test_verify_within_tolerance_window_accepted(client: TestClient) -> None:
    access_token = _register_and_login(client, email="tolerance@example.com")
    secret = _setup(client, access_token).json()["secret"]

    # counter_offset en vez de timedelta en segundos: da el paso exacto (30s) sin
    # depender de en qué punto del step de 30s caiga el "now" real al ejecutar el
    # test (evita flakiness en el borde del step).
    now = datetime.now(timezone.utc)
    past_code = pyotp.TOTP(secret).at(now, counter_offset=-1)
    response = client.post(
        "/2fa/verify", json={"code": past_code}, headers=_headers(access_token)
    )
    assert response.status_code == 200


def test_verify_future_within_tolerance_window_accepted(client: TestClient) -> None:
    access_token = _register_and_login(client, email="tolerancefuture@example.com")
    secret = _setup(client, access_token).json()["secret"]

    now = datetime.now(timezone.utc)
    future_code = pyotp.TOTP(secret).at(now, counter_offset=1)
    response = client.post(
        "/2fa/verify", json={"code": future_code}, headers=_headers(access_token)
    )
    assert response.status_code == 200


def test_verify_outside_tolerance_window_rejected(client: TestClient) -> None:
    access_token = _register_and_login(client, email="expired@example.com")
    secret = _setup(client, access_token).json()["secret"]

    # Dos pasos atrás: ya fuera de la ventana de tolerancia (valid_window=1 => ±1 paso).
    now = datetime.now(timezone.utc)
    expired_code = pyotp.TOTP(secret).at(now, counter_offset=-2)
    response = client.post(
        "/2fa/verify", json={"code": expired_code}, headers=_headers(access_token)
    )
    assert response.status_code == 401


def test_verify_future_outside_tolerance_window_rejected(client: TestClient) -> None:
    access_token = _register_and_login(client, email="expiredfuture@example.com")
    secret = _setup(client, access_token).json()["secret"]

    now = datetime.now(timezone.utc)
    future_code = pyotp.TOTP(secret).at(now, counter_offset=2)
    response = client.post(
        "/2fa/verify", json={"code": future_code}, headers=_headers(access_token)
    )
    assert response.status_code == 401


def test_verify_rejects_malformed_code(client: TestClient) -> None:
    access_token = _register_and_login(client, email="malformed@example.com")
    _setup(client, access_token)
    response = client.post(
        "/2fa/verify", json={"code": "abcdef"}, headers=_headers(access_token)
    )
    assert response.status_code == 422


def test_setup_rejects_missing_password_field(client: TestClient) -> None:
    access_token = _register_and_login(client, email="missingpw@example.com")
    response = client.post("/2fa/setup", json={}, headers=_headers(access_token))
    assert response.status_code == 422


def test_verify_lockout_after_five_failed_attempts(client: TestClient) -> None:
    access_token = _register_and_login(client, email="lockout@example.com")
    secret = _setup(client, access_token).json()["secret"]
    real_code = pyotp.TOTP(secret).now()
    wrong_code = "000000" if real_code != "000000" else "111111"

    for _ in range(5):
        response = client.post(
            "/2fa/verify", json={"code": wrong_code}, headers=_headers(access_token)
        )
        assert response.status_code == 401

    # Incluso con el código correcto, el bloqueo aplica antes de mirar el código.
    locked_response = client.post(
        "/2fa/verify", json={"code": real_code}, headers=_headers(access_token)
    )
    assert locked_response.status_code == 429
    # El detail confirma que este 429 lo dispara el lockout de TOTP (Fase 3,
    # Postgres) y no el rate limiter genérico de Fase 6 (que en /2fa/verify
    # está en el tier "general", capacidad 60 - no debería llegar a disparar
    # con solo 6 requests, pero el test debe demostrarlo, no asumirlo por el
    # código de estado).
    assert "Inténtalo de nuevo en" in locked_response.json()["detail"]


def test_verify_success_resets_failed_attempts(
    client: TestClient, db_session: Session
) -> None:
    access_token = _register_and_login(client, email="resetcounter@example.com")
    secret = _setup(client, access_token).json()["secret"]
    totp = pyotp.TOTP(secret)
    real_code = totp.now()
    wrong_code = "000000" if real_code != "000000" else "111111"

    client.post(
        "/2fa/verify", json={"code": wrong_code}, headers=_headers(access_token)
    )
    client.post(
        "/2fa/verify", json={"code": wrong_code}, headers=_headers(access_token)
    )

    ok_response = client.post(
        "/2fa/verify", json={"code": real_code}, headers=_headers(access_token)
    )
    assert ok_response.status_code == 200

    db_user = db_session.query(User).filter_by(email="resetcounter@example.com").one()
    assert db_user.totp_failed_attempts == 0


def test_verify_concurrent_wrong_attempts_lockout_not_bypassed(
    client: TestClient,
) -> None:
    """Cinco intentos fallidos SIMULTÁNEOS no deben poder "perder" incrementos del
    contador (lost update) y así saltarse el lockout. Verifica el fix de
    lock_user_for_totp_check (SELECT ... FOR UPDATE) en app/api/totp.py."""
    access_token = _register_and_login(client, email="concurrentlockout@example.com")
    secret = _setup(client, access_token).json()["secret"]
    real_code = pyotp.TOTP(secret).now()
    wrong_code = "000000" if real_code != "000000" else "111111"

    results: list[int] = []
    barrier = threading.Barrier(5)

    def _attempt() -> None:
        local_client = TestClient(app)
        barrier.wait(timeout=5)
        response = local_client.post(
            "/2fa/verify", json={"code": wrong_code}, headers=_headers(access_token)
        )
        results.append(response.status_code)

    threads = [threading.Thread(target=_attempt) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert results.count(401) == 5

    # Si el contador se hubiera perdido por la race, esta request pasaría de largo;
    # con el fix, el lockout ya debe estar activo tras los 5 fallos concurrentes.
    locked_response = client.post(
        "/2fa/verify", json={"code": real_code}, headers=_headers(access_token)
    )
    assert locked_response.status_code == 429
    # Ver comentario equivalente en test_verify_lockout_after_five_failed_attempts:
    # el detail distingue el 429 del lockout de TOTP del 429 del rate limiter.
    assert "Inténtalo de nuevo en" in locked_response.json()["detail"]


def test_verify_handles_corrupted_secret_as_controlled_error(
    client: TestClient, db_session: Session
) -> None:
    access_token = _register_and_login(client, email="corrupted@example.com")
    _setup(client, access_token)

    db_user = db_session.query(User).filter_by(email="corrupted@example.com").one()
    db_user.totp_secret_encrypted = "not-a-valid-fernet-token"
    db_session.commit()

    response = client.post(
        "/2fa/verify", json={"code": "123456"}, headers=_headers(access_token)
    )
    assert response.status_code == 500
    assert "detail" in response.json()
