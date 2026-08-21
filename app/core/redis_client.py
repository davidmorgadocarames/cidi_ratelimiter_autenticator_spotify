import redis

from app.core.config import settings

# Cliente Redis compartido a nivel de módulo (mismo patrón que app/db/session.py
# expone engine/SessionLocal): el proceso de FastAPI no hace fork, así que no
# aplica el footgun de Celery+prefork que sí exige crear el cliente
# perezosamente dentro de la tarea (ver app/worker.py). decode_responses=True
# es parte del contrato - app/services/recommendations.py y
# app/services/popular.py asumen str, no bytes, en las respuestas de Redis.
redis_client: redis.Redis = redis.from_url(  # type: ignore[no-untyped-call]
    settings.redis_url, decode_responses=True
)
