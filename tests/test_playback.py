from datetime import datetime, timedelta, timezone

import redis as redis_sync
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models.song import Song
from app.services.playback import get_playback_state, set_playback_state

PASSWORD = "supersecret"


def _register_and_login(
    client: TestClient, email: str, password: str = PASSWORD
) -> str:
    client.post("/auth/register", json={"email": email, "password": password})
    login_response = client.post(
        "/auth/login", data={"username": email, "password": password}
    )
    access_token: str = login_response.json()["access_token"]
    return access_token


def _headers(access_token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {access_token}"}


def _parse_iso(value: str) -> datetime:
    """Pydantic v2 serializa datetimes UTC con sufijo "Z" (visto en
    verificación manual: "...T07:00:18.699465Z"), pero
    datetime.fromisoformat() solo soporta "Z" desde Python 3.11 - la matriz
    de CI de este proyecto incluye 3.10 (ver .github/workflows/ci.yml), así
    que hay que normalizar "Z" a "+00:00" a mano para que los tests corran
    en las tres versiones."""
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _create_song_directly(
    db_session: Session,
    uploaded_by_id: int,
    status_value: str = "ready",
    title: str = "Canción de prueba",
) -> Song:
    song = Song(
        title=title,
        artist="Artista de prueba",
        uploaded_by_id=uploaded_by_id,
        status=status_value,
        original_object_key=f"original/playback-test/{title}",
        transcoded_object_key=(
            f"transcoded/playback-test/{title}" if status_value == "ready" else None
        ),
        content_type="audio/mpeg",
        file_size_bytes=123,
    )
    db_session.add(song)
    db_session.commit()
    db_session.refresh(song)
    return song


# --- set_playback_state / get_playback_state: contra Redis real ---


def test_playback_state_roundtrip(redis_client: redis_sync.Redis) -> None:
    state = {
        "song_id": 1,
        "position_seconds": 12.5,
        "is_playing": True,
        "device_id": "device-A",
        "updated_at": "2026-08-21T00:00:00+00:00",
    }

    set_playback_state(redis_client, user_id=777, state=state)

    assert get_playback_state(redis_client, 777) == state


def test_get_playback_state_returns_none_when_missing(
    redis_client: redis_sync.Redis,
) -> None:
    assert get_playback_state(redis_client, 999_999) is None


# --- PUT /users/me/playback ---


def test_put_playback_success_returns_state_with_server_timestamp(
    client: TestClient, db_session: Session
) -> None:
    token = _register_and_login(client, "playback-put@example.com")
    me_id = client.get("/auth/me", headers=_headers(token)).json()["id"]
    song = _create_song_directly(db_session, me_id)

    response = client.put(
        "/users/me/playback",
        json={
            "song_id": song.id,
            "position_seconds": 10.0,
            "is_playing": True,
            "device_id": "device-A",
        },
        headers=_headers(token),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["song_id"] == song.id
    assert body["position_seconds"] == 10.0
    assert body["is_playing"] is True
    assert body["device_id"] == "device-A"
    # No solo "el campo existe" (hallazgo de la revisión post-implementación:
    # el nombre del test prometía verificar el sellado del servidor, pero la
    # única aserción previa no distinguía eso de cualquier valor arbitrario) -
    # parsea el ISO y confirma que es un timestamp reciente de verdad.
    updated_at = _parse_iso(body["updated_at"])
    assert datetime.now(timezone.utc) - updated_at < timedelta(seconds=5)


def test_put_playback_ignores_client_supplied_updated_at(
    client: TestClient, db_session: Session
) -> None:
    """Prueba directa de la propiedad de seguridad central del campo: el
    servidor sella updated_at, nunca confía en lo que mande el cliente -
    aunque el cliente intente colar un valor viejo en el body (campo extra,
    fuera del schema PlaybackStateUpdate), la respuesta debe reflejar la hora
    real del servidor, no el valor colado."""
    token = _register_and_login(client, "playback-fake-timestamp@example.com")
    me_id = client.get("/auth/me", headers=_headers(token)).json()["id"]
    song = _create_song_directly(db_session, me_id)
    stale_timestamp = "2000-01-01T00:00:00+00:00"

    response = client.put(
        "/users/me/playback",
        json={
            "song_id": song.id,
            "position_seconds": 10.0,
            "is_playing": True,
            "device_id": "device-A",
            "updated_at": stale_timestamp,
        },
        headers=_headers(token),
    )

    assert response.status_code == 200
    updated_at = _parse_iso(response.json()["updated_at"])
    assert datetime.now(timezone.utc) - updated_at < timedelta(seconds=5)


def test_put_playback_rejects_nonexistent_song(
    client: TestClient, db_session: Session
) -> None:
    token = _register_and_login(client, "playback-404@example.com")

    response = client.put(
        "/users/me/playback",
        json={
            "song_id": 999_999,
            "position_seconds": 0,
            "is_playing": False,
            "device_id": "device-A",
        },
        headers=_headers(token),
    )

    assert response.status_code == 404


def test_put_playback_rejects_non_ready_song(
    client: TestClient, db_session: Session
) -> None:
    token = _register_and_login(client, "playback-409@example.com")
    me_id = client.get("/auth/me", headers=_headers(token)).json()["id"]
    song = _create_song_directly(db_session, me_id, status_value="processing")

    response = client.put(
        "/users/me/playback",
        json={
            "song_id": song.id,
            "position_seconds": 0,
            "is_playing": False,
            "device_id": "device-A",
        },
        headers=_headers(token),
    )

    assert response.status_code == 409


def test_put_playback_rejects_negative_position(
    client: TestClient, db_session: Session
) -> None:
    token = _register_and_login(client, "playback-negative@example.com")
    me_id = client.get("/auth/me", headers=_headers(token)).json()["id"]
    song = _create_song_directly(db_session, me_id)

    response = client.put(
        "/users/me/playback",
        json={
            "song_id": song.id,
            "position_seconds": -1,
            "is_playing": False,
            "device_id": "device-A",
        },
        headers=_headers(token),
    )

    assert response.status_code == 422


def test_put_playback_rejects_empty_device_id(
    client: TestClient, db_session: Session
) -> None:
    token = _register_and_login(client, "playback-empty-device@example.com")
    me_id = client.get("/auth/me", headers=_headers(token)).json()["id"]
    song = _create_song_directly(db_session, me_id)

    response = client.put(
        "/users/me/playback",
        json={
            "song_id": song.id,
            "position_seconds": 0,
            "is_playing": False,
            "device_id": "",
        },
        headers=_headers(token),
    )

    assert response.status_code == 422


def test_put_playback_requires_auth(client: TestClient) -> None:
    response = client.put(
        "/users/me/playback",
        json={
            "song_id": 1,
            "position_seconds": 0,
            "is_playing": False,
            "device_id": "device-A",
        },
    )
    assert response.status_code == 401


# --- GET /users/me/playback ---


def test_get_playback_404_before_any_put(client: TestClient) -> None:
    token = _register_and_login(client, "playback-get-404@example.com")

    response = client.get("/users/me/playback", headers=_headers(token))

    assert response.status_code == 404


def test_get_playback_returns_last_reported_state(
    client: TestClient, db_session: Session
) -> None:
    token = _register_and_login(client, "playback-get-200@example.com")
    me_id = client.get("/auth/me", headers=_headers(token)).json()["id"]
    song = _create_song_directly(db_session, me_id)
    client.put(
        "/users/me/playback",
        json={
            "song_id": song.id,
            "position_seconds": 42.0,
            "is_playing": True,
            "device_id": "device-A",
        },
        headers=_headers(token),
    )

    response = client.get("/users/me/playback", headers=_headers(token))

    assert response.status_code == 200
    body = response.json()
    assert body["song_id"] == song.id
    assert body["position_seconds"] == 42.0


def test_get_playback_requires_auth(client: TestClient) -> None:
    response = client.get("/users/me/playback")
    assert response.status_code == 401


# --- Escenario central: sincronización entre dispositivos ---


def test_second_device_put_overwrites_first_last_write_wins(
    client: TestClient, db_session: Session
) -> None:
    """El escenario central de esta fase: dos "dispositivos" (mismo usuario)
    reportan estado en secuencia - el segundo PUT sobreescribe al primero, y
    GET devuelve el estado del segundo (last-write-wins real, de punta a
    punta vía HTTP, no solo a nivel de servicio)."""
    token = _register_and_login(client, "playback-sync@example.com")
    me_id = client.get("/auth/me", headers=_headers(token)).json()["id"]
    song_a = _create_song_directly(db_session, me_id, title="Canción A")
    song_b = _create_song_directly(db_session, me_id, title="Canción B")

    client.put(
        "/users/me/playback",
        json={
            "song_id": song_a.id,
            "position_seconds": 5.0,
            "is_playing": True,
            "device_id": "device-A",
        },
        headers=_headers(token),
    )
    client.put(
        "/users/me/playback",
        json={
            "song_id": song_b.id,
            "position_seconds": 90.0,
            "is_playing": False,
            "device_id": "device-B",
        },
        headers=_headers(token),
    )

    response = client.get("/users/me/playback", headers=_headers(token))

    assert response.status_code == 200
    body = response.json()
    assert body["song_id"] == song_b.id
    assert body["device_id"] == "device-B"
    assert body["position_seconds"] == 90.0
    assert body["is_playing"] is False


def test_playback_state_isolated_between_users(
    client: TestClient, db_session: Session
) -> None:
    """Propiedad opuesta a la de GET /songs/popular (Fase 12): aquí cada
    usuario debe ver SOLO su propio estado, nunca el de otro."""
    token_a = _register_and_login(client, "playback-user-a@example.com")
    me_a_id = client.get("/auth/me", headers=_headers(token_a)).json()["id"]
    song_a = _create_song_directly(db_session, me_a_id, title="De A")

    token_b = _register_and_login(client, "playback-user-b@example.com")
    me_b_id = client.get("/auth/me", headers=_headers(token_b)).json()["id"]
    song_b = _create_song_directly(db_session, me_b_id, title="De B")

    client.put(
        "/users/me/playback",
        json={
            "song_id": song_a.id,
            "position_seconds": 1.0,
            "is_playing": True,
            "device_id": "device-A",
        },
        headers=_headers(token_a),
    )
    client.put(
        "/users/me/playback",
        json={
            "song_id": song_b.id,
            "position_seconds": 2.0,
            "is_playing": False,
            "device_id": "device-B",
        },
        headers=_headers(token_b),
    )

    response_a = client.get("/users/me/playback", headers=_headers(token_a))
    response_b = client.get("/users/me/playback", headers=_headers(token_b))

    assert response_a.json()["song_id"] == song_a.id
    assert response_b.json()["song_id"] == song_b.id
