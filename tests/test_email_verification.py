import re
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from conftest import decode_mailhog_body, get_mailhog_messages, mark_email_verified
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from app.models.user import User
from app.services.email import send_verification_email

PASSWORD = "supersecret"


def _register(client: TestClient, email: str, password: str = PASSWORD) -> None:
    response = client.post(
        "/auth/register", json={"email": email, "password": password}
    )
    assert response.status_code == 201


def _is_addressed_to(message: dict[str, Any], email: str) -> bool:
    recipients = {f"{to['Mailbox']}@{to['Domain']}" for to in message["To"] or []}
    return email in recipients


def _messages_for(email: str) -> list[dict[str, Any]]:
    return [m for m in get_mailhog_messages() if _is_addressed_to(m, email)]


def _message_for(email: str) -> dict[str, Any]:
    messages = _messages_for(email)
    assert messages, f"No se capturó ningún correo dirigido a {email}"
    return messages[0]


def _extract_token(message: dict[str, Any]) -> str:
    body = decode_mailhog_body(message)
    match = re.search(r"token=(\S+)", body)
    assert match is not None, f"No se encontró un token en el correo: {body!r}"
    return match.group(1)


def test_register_creates_unverified_user_and_sends_real_email(
    client: TestClient,
) -> None:
    email = "unverified@example.com"
    response = client.post(
        "/auth/register", json={"email": email, "password": PASSWORD}
    )
    assert response.status_code == 201
    assert response.json()["email_verified"] is False

    message = _message_for(email)
    body = decode_mailhog_body(message)
    assert "verify-email?token=" in body


def test_login_blocked_until_email_verified(client: TestClient) -> None:
    email = "gated@example.com"
    _register(client, email)

    response = client.post(
        "/auth/login", data={"username": email, "password": PASSWORD}
    )
    assert response.status_code == 403
    assert "Verifica tu email" in response.json()["detail"]


def test_verify_email_with_valid_token_unlocks_login(client: TestClient) -> None:
    email = "verify-ok@example.com"
    _register(client, email)
    token = _extract_token(_message_for(email))

    verify_response = client.get(f"/auth/verify-email?token={token}")
    assert verify_response.status_code == 200

    login_response = client.post(
        "/auth/login", data={"username": email, "password": PASSWORD}
    )
    assert login_response.status_code == 200


def test_verify_email_invalid_token_rejected(client: TestClient) -> None:
    response = client.get("/auth/verify-email?token=not-a-real-token")
    assert response.status_code == 400


def test_verify_email_expired_token_rejected(
    client: TestClient, db_session: Session
) -> None:
    email = "expired-token@example.com"
    _register(client, email)
    token = _extract_token(_message_for(email))

    user = db_session.query(User).filter(User.email == email).one()
    user.email_verification_token_expires_at = datetime.now(timezone.utc) - timedelta(
        seconds=1
    )
    db_session.commit()

    response = client.get(f"/auth/verify-email?token={token}")
    assert response.status_code == 400


def test_resend_verification_sends_new_token_and_invalidates_old_one(
    client: TestClient,
    test_session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # resend-verification agenda TODO su trabajo (lookup incluido) en
    # BackgroundTasks, que abre su propia sesión vía SessionLocal en vez de
    # Depends(get_db) (ver app/api/auth.py::_process_resend_verification) -
    # sin este monkeypatch, esa sesión apuntaría a la Postgres real de
    # desarrollo (settings.database_url), no a la de test, mismo tipo de
    # incidente que ya motivó test_session_factory para los scripts de
    # app/cli/.
    monkeypatch.setattr("app.api.auth.SessionLocal", test_session_factory)

    email = "resend@example.com"
    _register(client, email)
    old_token = _extract_token(_message_for(email))

    resend_response = client.post("/auth/resend-verification", json={"email": email})
    assert resend_response.status_code == 200

    # Dos correos capturados en total para este email: el del registro y el
    # del reenvío - el nuevo se identifica por traer un token distinto al
    # viejo (Mailhog no garantiza el orden de la lista).
    messages_to_email = _messages_for(email)
    assert len(messages_to_email) == 2
    tokens = {_extract_token(m) for m in messages_to_email}
    assert old_token in tokens
    new_token = next(t for t in tokens if t != old_token)

    # El token viejo ya no es válido (se sobreescribió en la fila del usuario).
    old_response = client.get(f"/auth/verify-email?token={old_token}")
    assert old_response.status_code == 400

    new_response = client.get(f"/auth/verify-email?token={new_token}")
    assert new_response.status_code == 200


def test_resend_verification_same_generic_response_regardless_of_account_state(
    client: TestClient,
    test_session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("app.api.auth.SessionLocal", test_session_factory)

    verified_email = "already-verified@example.com"
    _register(client, verified_email)
    mark_email_verified(verified_email)

    nonexistent_response = client.post(
        "/auth/resend-verification", json={"email": "ghost@example.com"}
    )
    verified_response = client.post(
        "/auth/resend-verification", json={"email": verified_email}
    )

    assert nonexistent_response.status_code == 200
    assert verified_response.status_code == 200
    assert nonexistent_response.json() == verified_response.json()


def test_send_verification_email_returns_false_when_smtp_unreachable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fuerza la rama except de send_verification_email apuntando a un puerto
    sin nada escuchando (mismo patrón que test_seed_catalog.py para
    catalog_server) - sin este test, esa rama queda fuera de
    --cov-fail-under=85."""
    monkeypatch.setattr("app.services.email.settings.smtp_host", "127.0.0.1")
    monkeypatch.setattr("app.services.email.settings.smtp_port", 1)

    result = send_verification_email("someone@example.com", "some-token")

    assert result is False
