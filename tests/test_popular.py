import json

import redis as redis_sync
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models.play import SongPlay
from app.models.song import Song
from app.services.popular import (
    _POPULAR_CACHE_KEY,
    fetch_popular_song_ids,
    get_popular_song_ids,
)

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
        original_object_key=f"original/popular-test/{title}",
        transcoded_object_key=(
            f"transcoded/popular-test/{title}" if status_value == "ready" else None
        ),
        content_type="audio/mpeg",
        file_size_bytes=123,
    )
    db_session.add(song)
    db_session.commit()
    db_session.refresh(song)
    return song


def _create_play(db_session: Session, user_id: int, song_id: int) -> None:
    db_session.add(SongPlay(user_id=user_id, song_id=song_id))
    db_session.commit()


# --- fetch_popular_song_ids: contra Postgres real ---


def test_fetch_popular_song_ids_ranks_by_play_count(
    client: TestClient, db_session: Session
) -> None:
    token = _register_and_login(client, "fetch-popular@example.com")
    me_id = client.get("/auth/me", headers=_headers(token)).json()["id"]
    less_popular = _create_song_directly(db_session, me_id, title="Menos popular")
    more_popular = _create_song_directly(db_session, me_id, title="Más popular")
    _create_play(db_session, me_id, less_popular.id)
    _create_play(db_session, me_id, more_popular.id)
    _create_play(db_session, me_id, more_popular.id)

    result = fetch_popular_song_ids(db_session, limit=10)

    assert result.index(more_popular.id) < result.index(less_popular.id)


def test_fetch_popular_song_ids_excludes_non_ready(
    client: TestClient, db_session: Session
) -> None:
    token = _register_and_login(client, "fetch-popular-notready@example.com")
    me_id = client.get("/auth/me", headers=_headers(token)).json()["id"]
    processing_song = _create_song_directly(
        db_session, me_id, status_value="processing"
    )
    _create_play(db_session, me_id, processing_song.id)

    result = fetch_popular_song_ids(db_session, limit=10)

    assert processing_song.id not in result


def test_fetch_popular_song_ids_respects_limit(
    client: TestClient, db_session: Session
) -> None:
    token = _register_and_login(client, "fetch-popular-limit@example.com")
    me_id = client.get("/auth/me", headers=_headers(token)).json()["id"]
    for i in range(5):
        song = _create_song_directly(db_session, me_id, title=f"Canción {i}")
        _create_play(db_session, me_id, song.id)

    result = fetch_popular_song_ids(db_session, limit=3)

    assert len(result) == 3


# --- get_popular_song_ids: cache-aside real contra Redis ---


def test_get_popular_song_ids_cache_miss_then_hit(
    client: TestClient, db_session: Session, redis_client: redis_sync.Redis
) -> None:
    """No solo prueba que "funciona" - prueba que la segunda llamada lee de
    Redis de verdad, no que recalcula: tras el primer llamado (cache miss),
    se manipula la clave directamente con un payload distinguible que
    Postgres jamás produciría, y se confirma que la segunda llamada
    devuelve ESE payload."""
    token = _register_and_login(client, "cache-miss-hit@example.com")
    me_id = client.get("/auth/me", headers=_headers(token)).json()["id"]
    song = _create_song_directly(db_session, me_id)
    _create_play(db_session, me_id, song.id)

    first = get_popular_song_ids(db_session, redis_client, limit=10)
    assert song.id in first

    sentinel_ids = [999_001, 999_002]
    redis_client.set(_POPULAR_CACHE_KEY, json.dumps(sentinel_ids), ex=300)

    second = get_popular_song_ids(db_session, redis_client, limit=10)

    assert second == sentinel_ids


def test_get_popular_song_ids_recomputes_after_cache_deleted(
    client: TestClient, db_session: Session, redis_client: redis_sync.Redis
) -> None:
    """Simula expiración borrando la clave directamente (sin sleep real,
    mismo criterio ya usado en otros tests de este proyecto para TTLs)."""
    token = _register_and_login(client, "cache-expiry@example.com")
    me_id = client.get("/auth/me", headers=_headers(token)).json()["id"]
    song = _create_song_directly(db_session, me_id)
    _create_play(db_session, me_id, song.id)

    get_popular_song_ids(db_session, redis_client, limit=10)
    redis_client.delete(_POPULAR_CACHE_KEY)

    result = get_popular_song_ids(db_session, redis_client, limit=10)

    assert song.id in result


def test_get_popular_song_ids_caches_at_max_not_at_requested_limit(
    client: TestClient, db_session: Session, redis_client: redis_sync.Redis
) -> None:
    """Una primera request con limit pequeño no debe dejar un caché corto
    que no pueda servir una request posterior con limit mayor (detalle
    deliberado del diseño, ver app/services/popular.py)."""
    token = _register_and_login(client, "cache-max@example.com")
    me_id = client.get("/auth/me", headers=_headers(token)).json()["id"]
    for i in range(5):
        song = _create_song_directly(db_session, me_id, title=f"Canción {i}")
        _create_play(db_session, me_id, song.id)

    small = get_popular_song_ids(db_session, redis_client, limit=2)
    large = get_popular_song_ids(db_session, redis_client, limit=5)

    assert len(small) == 2
    assert len(large) == 5


def test_get_popular_song_ids_degrades_gracefully_when_redis_down(
    client: TestClient, db_session: Session
) -> None:
    """Hallazgo de la revisión post-implementación: a diferencia de las
    recomendaciones de la Fase 11 (sin ningún camino alternativo si Redis
    falla), aquí SÍ hay una alternativa real y barata - Postgres, la misma
    que ya se consulta en cada cache miss - así que un Redis caído degrada
    a "calcular siempre en vivo" en vez de propagar el error."""
    token = _register_and_login(client, "redis-down@example.com")
    me_id = client.get("/auth/me", headers=_headers(token)).json()["id"]
    song = _create_song_directly(db_session, me_id)
    _create_play(db_session, me_id, song.id)

    def _boom(*args: object, **kwargs: object) -> None:
        raise redis_sync.RedisError("Redis simulado caído")

    class _BrokenRedis:
        get = _boom
        set = _boom

    result = get_popular_song_ids(db_session, _BrokenRedis(), limit=10)  # type: ignore[arg-type]

    assert song.id in result


# --- GET /songs/popular ---


def test_popular_endpoint_ranks_correctly_and_respects_limit(
    client: TestClient, db_session: Session
) -> None:
    token = _register_and_login(client, "endpoint-popular@example.com")
    me_id = client.get("/auth/me", headers=_headers(token)).json()["id"]
    less_popular = _create_song_directly(db_session, me_id, title="Menos popular")
    more_popular = _create_song_directly(db_session, me_id, title="Más popular")
    _create_play(db_session, me_id, less_popular.id)
    _create_play(db_session, me_id, more_popular.id)
    _create_play(db_session, me_id, more_popular.id)

    response = client.get(
        "/songs/popular", params={"limit": 1}, headers=_headers(token)
    )

    assert response.status_code == 200
    body = response.json()
    assert len(body) == 1
    assert body[0]["id"] == more_popular.id


def test_popular_endpoint_excludes_non_ready(
    client: TestClient, db_session: Session
) -> None:
    token = _register_and_login(client, "endpoint-popular-notready@example.com")
    me_id = client.get("/auth/me", headers=_headers(token)).json()["id"]
    processing_song = _create_song_directly(
        db_session, me_id, status_value="processing"
    )
    _create_play(db_session, me_id, processing_song.id)
    response = client.get("/songs/popular", headers=_headers(token))

    assert response.status_code == 200
    returned_ids = [song["id"] for song in response.json()]
    assert processing_song.id not in returned_ids


def test_popular_endpoint_ignores_stale_cached_ids(
    client: TestClient, db_session: Session, redis_client: redis_sync.Redis
) -> None:
    """Hallazgo de la revisión post-implementación: el filtro defensivo del
    endpoint (status="ready" en la relectura + omisión de IDs colgantes)
    nunca se ejercitaba en ningún test, porque fetch_popular_song_ids ya
    filtra "ready" al escribir el caché - una canción "processing" nunca
    llega a cachearse en el flujo normal. Aquí se envenena el caché
    DIRECTAMENTE (simulando una canción que pasara a no-"ready" o se
    borrara mientras seguía cacheada) para probar de verdad esa segunda
    capa de defensa, no solo la primera."""
    token = _register_and_login(client, "endpoint-popular-stale@example.com")
    me_id = client.get("/auth/me", headers=_headers(token)).json()["id"]
    ready_song = _create_song_directly(db_session, me_id, title="Sigue viva")
    stale_song = _create_song_directly(
        db_session, me_id, status_value="processing", title="Ya no ready"
    )
    dangling_id = 999_999

    redis_client.set(
        _POPULAR_CACHE_KEY,
        json.dumps([stale_song.id, ready_song.id, dangling_id]),
        ex=300,
    )

    response = client.get("/songs/popular", headers=_headers(token))

    assert response.status_code == 200
    returned_ids = [song["id"] for song in response.json()]
    assert returned_ids == [ready_song.id]
    assert stale_song.id not in returned_ids
    assert dangling_id not in returned_ids


def test_popular_endpoint_same_list_for_different_users(
    client: TestClient, db_session: Session
) -> None:
    """Sin personalización a propósito - "popular" es la misma lista para
    cualquier usuario, a diferencia de /users/me/recommendations."""
    token_a = _register_and_login(client, "popular-user-a@example.com")
    me_a_id = client.get("/auth/me", headers=_headers(token_a)).json()["id"]
    song = _create_song_directly(db_session, me_a_id)
    _create_play(db_session, me_a_id, song.id)

    token_b = _register_and_login(client, "popular-user-b@example.com")

    response_a = client.get("/songs/popular", headers=_headers(token_a))
    response_b = client.get("/songs/popular", headers=_headers(token_b))

    assert response_a.json() == response_b.json()


def test_popular_endpoint_requires_auth(client: TestClient) -> None:
    response = client.get("/songs/popular")
    assert response.status_code == 401


def test_popular_endpoint_rejects_invalid_limit(client: TestClient) -> None:
    token = _register_and_login(client, "popular-invalid-limit@example.com")

    response = client.get(
        "/songs/popular", params={"limit": -1}, headers=_headers(token)
    )

    assert response.status_code == 422
