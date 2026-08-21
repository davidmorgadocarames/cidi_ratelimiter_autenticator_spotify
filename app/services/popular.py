import json
import logging
from typing import cast

import redis
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.play import SongPlay
from app.models.song import Song

logger = logging.getLogger(__name__)

# Clave GLOBAL única (no una por usuario, a diferencia de
# "recommendations:{user_id}" de la Fase 11): "popular" es la misma lista
# para todo el mundo, sin personalización.
_POPULAR_CACHE_KEY = "songs:popular"
_POPULAR_CACHE_MAX = 100
# 5 min - mismo ritmo que el intervalo de Celery Beat de la Fase 11
# (coincidencia deliberada por consistencia, no una dependencia real entre
# ambas fases: esta caché no toca Celery en absoluto).
_POPULAR_CACHE_TTL_SECONDS = 5 * 60


def fetch_popular_song_ids(db: Session, limit: int) -> list[int]:
    """Una única query agregada, global - a diferencia de
    fetch_play_aggregates (Fase 11), que agrega TODO song_plays para poder
    derivar afinidad por-usuario, aquí no hace falta esa estructura completa:
    solo el ranking de conteo total por canción."""
    rows = db.execute(
        select(SongPlay.song_id)
        .join(Song, Song.id == SongPlay.song_id)
        .where(Song.status == "ready")
        .group_by(SongPlay.song_id)
        .order_by(func.count().desc())
        .limit(limit)
    ).all()
    return [row[0] for row in rows]


def get_popular_song_ids(
    db: Session, redis_client: redis.Redis, limit: int
) -> list[int]:
    """Cache-aside clásico: lee Redis primero; en cache miss, calcula bajo
    demanda y guarda con TTL - a diferencia de las recomendaciones de la
    Fase 11 (precalculadas por Celery Beat en background), aquí el cálculo
    ocurre en el propio request que encuentra el caché vacío/expirado.

    Siempre cachea al tamaño MÁXIMO (_POPULAR_CACHE_MAX), nunca al `limit`
    pedido, y recorta aquí en la lectura - así una primera request con
    limit=10 no deja un caché corto que no pueda servir una request
    posterior con limit=50.

    Degrada con gracia si Redis no responde (hallazgo de la revisión post-
    implementación): a diferencia de las recomendaciones de la Fase 11 (que
    no tienen ningún camino alternativo si Redis falla, porque el cálculo
    solo ocurre en la tarea de Celery), aquí SÍ hay una alternativa real y
    barata a mano - Postgres, la misma que ya se consulta en cada cache
    miss - así que un Redis caído degrada a "calcular siempre en vivo" en
    vez de un 500."""
    try:
        cached = cast("str | None", redis_client.get(_POPULAR_CACHE_KEY))
    except redis.RedisError:
        logger.exception("Redis no respondió leyendo el caché de popular")
        cached = None

    if cached is not None:
        song_ids: list[int] = json.loads(cached)
    else:
        song_ids = fetch_popular_song_ids(db, _POPULAR_CACHE_MAX)
        try:
            redis_client.set(
                _POPULAR_CACHE_KEY,
                json.dumps(song_ids),
                ex=_POPULAR_CACHE_TTL_SECONDS,
            )
        except redis.RedisError:
            logger.exception("Redis no respondió escribiendo el caché de popular")
    return song_ids[:limit]
