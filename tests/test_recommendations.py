import pytest
import redis as redis_sync
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.play import SongPlay
from app.models.song import Song
from app.services.recommendations import (
    PlayAggregates,
    SongMeta,
    fetch_play_aggregates,
    fetch_ready_songs,
    get_recommendations,
    rank_recommendations_for_user,
    store_recommendations,
)
from app.worker import _run_recompute, celery_app

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
    artist: str,
    status_value: str = "ready",
    title: str = "Canción de prueba",
) -> Song:
    """Mismo patrón que _create_song_directly de test_songs.py, con un
    parámetro extra de artist (necesario para probar afinidad).
    transcoded_object_key se rellena con un valor dummy para status="ready" -
    GET /songs/{id}/stream lo exige (500 si es None con status="ready", ver
    app/api/songs.py) y generate_presigned_url no valida que el objeto exista
    de verdad en MinIO, solo construye la URL firmada."""
    song = Song(
        title=title,
        artist=artist,
        uploaded_by_id=uploaded_by_id,
        status=status_value,
        original_object_key=f"original/reco-test/{artist}/{title}",
        transcoded_object_key=(
            f"transcoded/reco-test/{artist}/{title}"
            if status_value == "ready"
            else None
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


# --- rank_recommendations_for_user: función pura, sin Postgres ---


def test_rank_prioritizes_artist_affinity_over_already_played() -> None:
    ready_songs = [
        SongMeta(id=1, artist="Artist A"),
        SongMeta(id=2, artist="Artist A"),
        SongMeta(id=3, artist="Artist B"),
    ]
    aggregates = PlayAggregates(
        user_artist_scores={1: {"Artist A": 3}},
        user_played_song_ids={1: {1}},
        song_global_play_counts={1: 3, 3: 5},
    )

    result = rank_recommendations_for_user(1, ready_songs, aggregates)

    assert result[0] == 2
    assert 1 not in result


def test_rank_falls_back_to_global_popularity_when_affinity_exhausted() -> None:
    ready_songs = [
        SongMeta(id=1, artist="Artist A"),
        SongMeta(id=2, artist="Artist B"),
        SongMeta(id=3, artist="Artist C"),
    ]
    aggregates = PlayAggregates(
        user_artist_scores={1: {"Artist A": 1}},
        user_played_song_ids={1: {1}},
        song_global_play_counts={2: 5, 3: 1},
    )

    result = rank_recommendations_for_user(1, ready_songs, aggregates)

    assert result == [2, 3]


def test_rank_cold_start_user_falls_back_to_global_popularity() -> None:
    ready_songs = [SongMeta(id=1, artist="A"), SongMeta(id=2, artist="B")]
    aggregates = PlayAggregates(song_global_play_counts={2: 3, 1: 1})

    result = rank_recommendations_for_user(999, ready_songs, aggregates)

    assert result == [2, 1]


def test_rank_no_plays_anywhere_falls_back_to_most_recent() -> None:
    # ready_songs ya viene ordenada por created_at desc (contrato de
    # fetch_ready_songs) - aquí se construye a mano ya en ese orden.
    ready_songs = [SongMeta(id=5, artist="A"), SongMeta(id=6, artist="B")]
    aggregates = PlayAggregates()

    result = rank_recommendations_for_user(1, ready_songs, aggregates)

    assert result == [5, 6]


def test_rank_respects_limit() -> None:
    ready_songs = [SongMeta(id=i, artist="A") for i in range(1, 31)]
    aggregates = PlayAggregates(
        song_global_play_counts={i: 31 - i for i in range(1, 31)}
    )

    result = rank_recommendations_for_user(1, ready_songs, aggregates, limit=20)

    assert len(result) == 20


# --- fetch_ready_songs / fetch_play_aggregates: contra Postgres real ---


def test_fetch_ready_songs_excludes_non_ready(
    client: TestClient, db_session: Session
) -> None:
    token = _register_and_login(client, "fetch-ready@example.com")
    me_id = client.get("/auth/me", headers=_headers(token)).json()["id"]
    ready_song = _create_song_directly(db_session, me_id, artist="A")
    _create_song_directly(db_session, me_id, artist="B", status_value="processing")

    songs = fetch_ready_songs(db_session)

    ids = {song.id for song in songs}
    assert ready_song.id in ids
    assert len(songs) == 1


def test_fetch_play_aggregates_reflects_real_plays(
    client: TestClient, db_session: Session
) -> None:
    token = _register_and_login(client, "fetch-agg@example.com")
    me_id = client.get("/auth/me", headers=_headers(token)).json()["id"]
    song = _create_song_directly(db_session, me_id, artist="Artist X")
    # Dos plays de la MISMA canción - repeat, no un duplicado a descartar.
    _create_play(db_session, me_id, song.id)
    _create_play(db_session, me_id, song.id)

    aggregates = fetch_play_aggregates(db_session)

    assert aggregates.user_artist_scores[me_id]["Artist X"] == 2
    assert song.id in aggregates.user_played_song_ids[me_id]
    assert aggregates.song_global_play_counts[song.id] == 2


# --- store_recommendations / get_recommendations: contra Redis real ---


def test_store_and_get_recommendations_roundtrip(
    redis_client: redis_sync.Redis,
) -> None:
    store_recommendations(redis_client, user_id=555, song_ids=[3, 1, 2])

    assert get_recommendations(redis_client, 555) == [3, 1, 2]


def test_get_recommendations_returns_none_when_missing(
    redis_client: redis_sync.Redis,
) -> None:
    assert get_recommendations(redis_client, 999_999) is None


# --- recompute_all_recommendations (la task, llamada directo) ---


def test_recompute_all_recommendations_stores_per_user_keys(
    client: TestClient, db_session: Session, redis_client: redis_sync.Redis
) -> None:
    token_a = _register_and_login(client, "recompute-a@example.com")
    me_a_id = client.get("/auth/me", headers=_headers(token_a)).json()["id"]
    token_b = _register_and_login(client, "recompute-b@example.com")
    me_b_id = client.get("/auth/me", headers=_headers(token_b)).json()["id"]

    played_song = _create_song_directly(db_session, me_a_id, artist="Shared Artist")
    other_song = _create_song_directly(db_session, me_a_id, artist="Shared Artist")
    _create_play(db_session, me_a_id, played_song.id)

    _run_recompute(settings.test_database_url)

    reco_a = get_recommendations(redis_client, me_a_id)
    reco_b = get_recommendations(redis_client, me_b_id)
    assert reco_a is not None
    assert other_song.id in reco_a
    assert played_song.id not in reco_a
    # Usuario B (cold start, sin plays): tiene su propia clave igual, aunque
    # sea el fallback de popularidad/recientes - no queda sin procesar.
    assert reco_b is not None


def test_beat_schedule_task_name_matches_registered_task() -> None:
    """Hallazgo de la revisión del plan: sin esto, un typo o un rename en el
    nombre de la tarea programada en Beat quedaría sin detectar hasta
    producción (Beat mandaría "unregistered task" en runtime) - invisible
    tanto para los tests que llaman la función directo como para el smoke
    test de CI que solo mira si el proceso del worker sigue vivo."""
    scheduled_task_name = celery_app.conf.beat_schedule["recompute-recommendations"][
        "task"
    ]
    assert scheduled_task_name in celery_app.tasks


# --- GET /users/me/recommendations ---


def test_recommendations_endpoint_empty_before_first_recompute(
    client: TestClient,
) -> None:
    token = _register_and_login(client, "endpoint-empty@example.com")

    response = client.get("/users/me/recommendations", headers=_headers(token))

    assert response.status_code == 200
    assert response.json() == []


def test_recommendations_endpoint_returns_precalculated_list_in_order(
    client: TestClient, db_session: Session
) -> None:
    token = _register_and_login(client, "endpoint-full@example.com")
    me_id = client.get("/auth/me", headers=_headers(token)).json()["id"]
    song_a = _create_song_directly(db_session, me_id, artist="Shared", title="A")
    song_b = _create_song_directly(db_session, me_id, artist="Shared", title="B")
    played = _create_song_directly(db_session, me_id, artist="Shared", title="C")
    _create_play(db_session, me_id, played.id)

    _run_recompute(settings.test_database_url)

    response = client.get("/users/me/recommendations", headers=_headers(token))

    assert response.status_code == 200
    body = response.json()
    returned_ids = [song["id"] for song in body]
    assert played.id not in returned_ids
    assert {song_a.id, song_b.id} <= set(returned_ids)


def test_recommendations_endpoint_requires_auth(client: TestClient) -> None:
    response = client.get("/users/me/recommendations")
    assert response.status_code == 401


# --- Registro de plays en GET /songs/{id}/stream ---


def test_stream_url_records_a_play(client: TestClient, db_session: Session) -> None:
    token = _register_and_login(client, "stream-play@example.com")
    me_id = client.get("/auth/me", headers=_headers(token)).json()["id"]
    song = _create_song_directly(db_session, me_id, artist="Streamer")

    response = client.get(f"/songs/{song.id}/stream", headers=_headers(token))

    assert response.status_code == 200
    plays = db_session.query(SongPlay).filter_by(user_id=me_id, song_id=song.id).all()
    assert len(plays) == 1


def test_stream_url_succeeds_even_if_play_recording_fails(
    client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Best-effort (hallazgo de la revisión del plan): un fallo específico al
    registrar el play no debe romper una función núcleo (reproducir audio)
    que hoy es puramente de lectura."""
    token = _register_and_login(client, "stream-play-fail@example.com")
    me_id = client.get("/auth/me", headers=_headers(token)).json()["id"]
    song = _create_song_directly(db_session, me_id, artist="Streamer Fail")

    def _boom(self: Session, instance: object) -> None:
        raise RuntimeError("Postgres simulado explotando en el insert del play")

    monkeypatch.setattr(Session, "add", _boom)

    response = client.get(f"/songs/{song.id}/stream", headers=_headers(token))

    assert response.status_code == 200
    assert "url" in response.json()
