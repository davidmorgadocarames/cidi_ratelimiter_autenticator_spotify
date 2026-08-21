import json
from typing import Any, cast

import redis

# Una clave por usuario, mismo namespacing por prefijo que
# "recommendations:{user_id}" (Fase 11) - a diferencia de esa clave, aquí NO
# hay gestión de dispositivos: es un único puntero global "reproduciendo
# ahora" por usuario, compartido entre todos sus dispositivos, no un estado
# independiente por cada uno (ver docs/architecture.md).
_PLAYBACK_KEY_PREFIX = "playback:"
# 24h: generoso para sobrevivir una pausa normal de uso sin necesitar
# actividad real para mantenerse vivo, pero acotado para no quedar
# indefinidamente obsoleto si el usuario deja de usar la app. Se refresca en
# cada escritura.
_PLAYBACK_TTL_SECONDS = 24 * 60 * 60


def set_playback_state(
    redis_client: redis.Redis, user_id: int, state: dict[str, Any]
) -> None:
    """`state` debe llegar con "updated_at" ya como string ISO 8601, no un
    objeto datetime - json.dumps no serializa datetime por defecto (lanzaría
    TypeError). El endpoint lo construye vía
    PlaybackState(...).model_dump(mode="json")."""
    redis_client.set(
        f"{_PLAYBACK_KEY_PREFIX}{user_id}",
        json.dumps(state),
        ex=_PLAYBACK_TTL_SECONDS,
    )


def get_playback_state(
    redis_client: redis.Redis, user_id: int
) -> dict[str, Any] | None:
    raw = cast("str | None", redis_client.get(f"{_PLAYBACK_KEY_PREFIX}{user_id}"))
    if raw is None:
        return None
    state: dict[str, Any] = json.loads(raw)
    return state
