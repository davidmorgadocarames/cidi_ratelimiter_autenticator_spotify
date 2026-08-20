import logging
import uuid
from datetime import timedelta

import redis
from celery import Celery
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.core.config import settings
from app.models.user import User
from app.services.recommendations import (
    fetch_play_aggregates,
    fetch_ready_songs,
    rank_recommendations_for_user,
    store_recommendations,
)

logger = logging.getLogger(__name__)

celery_app = Celery("cidi", broker=settings.celery_broker_url)
# Sin result backend: nada llama a .get() sobre el resultado de esta tarea -
# Beat la dispara, el efecto es la escritura a Redis (store_recommendations),
# un backend sería una pieza más sin ningún consumidor.
celery_app.conf.beat_schedule = {
    "recompute-recommendations": {
        "task": "app.worker.recompute_all_recommendations",
        "schedule": timedelta(minutes=5),
    }
}

_RECOMPUTE_LOCK_KEY = "recompute:lock"
# < 5 min (el intervalo del beat_schedule de arriba): si el proceso muere a
# mitad de un pase, el próximo ciclo de Beat encuentra el lock ya expirado en
# vez de quedarse bloqueado indefinidamente.
_RECOMPUTE_LOCK_TTL_SECONDS = 240

# Compare-and-delete atómico (mismo estilo que el script Lua del rate limiter,
# app/core/rate_limiter.py): sin esto, un DEL incondicional podría borrar el
# lock de un pase MÁS NUEVO si el pase actual excede el TTL (el lock expira
# solo, otro pase lo adquiere, y el pase viejo lo borra igual al terminar) -
# hallazgo real de la revisión post-implementación, confirmado por dos
# revisores independientes. El token único (uuid por ejecución) es lo que
# permite distinguir "mi lock" de "un lock ajeno que ya lo reemplazó".
_RELEASE_LOCK_SCRIPT = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
    return redis.call('DEL', KEYS[1])
else
    return 0
end
"""


def _run_recompute(database_url: str) -> None:
    """Lógica real, separada del wrapper de Celery de abajo para poder
    testearla contra TEST_DATABASE_URL en vez de DATABASE_URL (ver
    tests/test_recommendations.py) - si los tests llamaran directo a la task
    decorada, crearía su engine contra la base de datos de producción/dev
    (settings.database_url), no la de test.

    El engine de Postgres y el cliente Redis se crean AQUÍ DENTRO, no a nivel
    de módulo: `celery worker` usa por defecto el pool "prefork" (un proceso
    por CPU, vía fork()) - un engine SQLAlchemy creado en el proceso padre
    antes del fork comparte sockets de conexión corruptos entre los procesos
    hijo (footgun conocido de Celery+SQLAlchemy). Crearlo aquí, una vez por
    ejecución, evita el problema sin necesitar wiring de señales de Celery
    (`worker_process_init`) - aceptable porque esta tarea corre cada 5 min,
    no por request."""
    redis_client = redis.Redis.from_url(settings.redis_url, decode_responses=True)

    # Lock corto: Celery Beat no deduplica ejecuciones solapadas por sí solo -
    # sin esto, si un pase alguna vez tardara más que el intervalo de 5 min,
    # se acumularía un backlog creciente de pases completos sobre el mismo
    # worker. Con la agregación de una sola query por ciclo (ver
    # fetch_play_aggregates) un pase es rápido incluso con cientos de
    # usuarios, pero el lock es una red de seguridad real, no cosmética.
    # Token único (no un valor constante) para poder liberar solo NUESTRO
    # lock más abajo, no el de un pase más nuevo que ya lo haya reemplazado.
    lock_token = uuid.uuid4().hex
    acquired = redis_client.set(
        _RECOMPUTE_LOCK_KEY, lock_token, nx=True, ex=_RECOMPUTE_LOCK_TTL_SECONDS
    )
    if not acquired:
        logger.info(
            "recompute_all_recommendations: ya hay un pase en curso, se "
            "salta este ciclo"
        )
        return

    try:
        # create_engine() DENTRO de este try (no antes de adquirir el lock ni
        # fuera de él): si fallara (ej. URL mal formada), el lock igual se
        # libera en el finally de abajo en vez de quedar retenido hasta el
        # TTL y repetir el fallo en cada ciclo (hallazgo de la revisión
        # post-implementación).
        engine = create_engine(database_url)
        session_factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
        db = session_factory()
        try:
            ready_songs = fetch_ready_songs(db)
            aggregates = fetch_play_aggregates(db)
            user_ids = db.scalars(select(User.id)).all()
            for user_id in user_ids:
                song_ids = rank_recommendations_for_user(
                    user_id, ready_songs, aggregates
                )
                store_recommendations(redis_client, user_id, song_ids)
            logger.info(
                "recompute_all_recommendations: %d usuarios procesados",
                len(user_ids),
            )
        finally:
            db.close()
            engine.dispose()
    finally:
        redis_client.eval(_RELEASE_LOCK_SCRIPT, 1, _RECOMPUTE_LOCK_KEY, lock_token)


@celery_app.task(name="app.worker.recompute_all_recommendations")
def recompute_all_recommendations() -> None:
    _run_recompute(settings.database_url)
