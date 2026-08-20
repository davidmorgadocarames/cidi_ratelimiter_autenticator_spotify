import json
from dataclasses import dataclass, field
from typing import cast

import redis
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.play import SongPlay
from app.models.song import Song

_RECOMMENDATION_LIMIT = 20
# 3x el intervalo de recálculo de Celery Beat (5 min, ver app/worker.py) - red
# de seguridad: si Beat dejara de correr, las recomendaciones expiran solas en
# vez de servirse indefinidamente obsoletas.
_RECOMMENDATIONS_TTL_SECONDS = 15 * 60


@dataclass(frozen=True)
class SongMeta:
    id: int
    artist: str


@dataclass
class PlayAggregates:
    user_artist_scores: dict[int, dict[str, int]] = field(default_factory=dict)
    user_played_song_ids: dict[int, set[int]] = field(default_factory=dict)
    song_global_play_counts: dict[int, int] = field(default_factory=dict)


def fetch_ready_songs(db: Session) -> list[SongMeta]:
    """Ordenadas por created_at desc - ese mismo orden sirve como fallback
    final de "más recientes" en rank_recommendations_for_user, sin necesitar
    una query aparte."""
    rows = db.execute(
        select(Song.id, Song.artist)
        .where(Song.status == "ready")
        .order_by(Song.created_at.desc())
    ).all()
    return [SongMeta(id=row.id, artist=row.artist) for row in rows]


def fetch_play_aggregates(db: Session) -> PlayAggregates:
    """Una única query sobre toda song_plays (join a songs.artist), agregada
    en Python en un solo paso. Reemplaza tanto la afinidad por-usuario como el
    fallback de popularidad global - ambos salen de la MISMA lectura, una vez
    por ciclo de recompute_all_recommendations, no una vez por usuario
    (rediseño de la revisión del plan de la Fase 11: la primera versión hacía
    esta agregación por usuario, degradando linealmente con el número de
    usuarios)."""
    rows = db.execute(
        select(SongPlay.user_id, SongPlay.song_id, Song.artist).join(
            Song, Song.id == SongPlay.song_id
        )
    ).all()

    aggregates = PlayAggregates()
    for user_id, song_id, artist in rows:
        artist_scores = aggregates.user_artist_scores.setdefault(user_id, {})
        artist_scores[artist] = artist_scores.get(artist, 0) + 1
        aggregates.user_played_song_ids.setdefault(user_id, set()).add(song_id)
        aggregates.song_global_play_counts[song_id] = (
            aggregates.song_global_play_counts.get(song_id, 0) + 1
        )
    return aggregates


def rank_recommendations_for_user(
    user_id: int,
    ready_songs: list[SongMeta],
    aggregates: PlayAggregates,
    limit: int = _RECOMMENDATION_LIMIT,
) -> list[int]:
    """Función pura, sin Postgres - heurística de conteo simple, no
    collaborative filtering ni ML:
    1. Afinidad por artista: canciones "ready" de artistas que el usuario ya
       escuchó, excluyendo las ya reproducidas, ordenadas por el score de
       afinidad del artista (desempate: plays globales de la canción).
    2. Si no llenan `limit`: fallback a popularidad global (excluyendo ya
       reproducidas), solo canciones con al menos un play real - no rellena
       con ceros.
    3. Si sigue sin llenar (o el usuario/sistema no tiene ningún play): más
       recientes (`ready_songs` ya viene ordenada por created_at desc)."""
    played = aggregates.user_played_song_ids.get(user_id, set())
    artist_scores = aggregates.user_artist_scores.get(user_id, {})
    global_counts = aggregates.song_global_play_counts

    result: list[int] = []
    seen: set[int] = set()

    def _add(song_id: int) -> bool:
        if song_id not in seen:
            result.append(song_id)
            seen.add(song_id)
        return len(result) >= limit

    if artist_scores:
        candidates = [
            song
            for song in ready_songs
            if song.artist in artist_scores and song.id not in played
        ]
        candidates.sort(
            key=lambda s: (artist_scores[s.artist], global_counts.get(s.id, 0)),
            reverse=True,
        )
        for song in candidates:
            if _add(song.id):
                return result

    if global_counts:
        popular = [
            song
            for song in ready_songs
            if song.id not in played and global_counts.get(song.id, 0) > 0
        ]
        popular.sort(key=lambda s: global_counts.get(s.id, 0), reverse=True)
        for song in popular:
            if _add(song.id):
                return result

    for song in ready_songs:
        if song.id in played:
            continue
        if _add(song.id):
            return result

    return result


def store_recommendations(
    redis_client: redis.Redis, user_id: int, song_ids: list[int]
) -> None:
    redis_client.set(
        f"recommendations:{user_id}",
        json.dumps(song_ids),
        ex=_RECOMMENDATIONS_TTL_SECONDS,
    )


def get_recommendations(redis_client: redis.Redis, user_id: int) -> list[int] | None:
    # redis-py tipa .get() como Awaitable[Any] | Any (mixin compartido con el
    # cliente async) - en runtime, con el cliente síncrono y
    # decode_responses=True, siempre es str | None.
    raw = cast("str | None", redis_client.get(f"recommendations:{user_id}"))
    if raw is None:
        return None
    ids: list[int] = json.loads(raw)
    return ids
