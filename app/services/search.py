import logging
from typing import Any

from meilisearch import Client
from meilisearch.errors import MeilisearchApiError

from app.core.config import settings
from app.models.song import Song

logger = logging.getLogger(__name__)

_INDEX_NAME = "songs"
# 10s, no 5s: bajo carga de CI (matriz de 3 versiones de Python compartiendo
# runner) una task puede tardar más - un timeout corto aquí no hace fallar la
# subida (index_song sigue siendo best-effort), pero SÍ puede dejar la canción
# sin indexar todavía cuando corre inmediatamente después un test que asume
# searchabilidad instantánea (hallazgo de la revisión post-implementación).
_TASK_TIMEOUT_MS = 10000

# Timeout corto y explícito: sin esto, un Meilisearch inalcanzable podría dejar
# una request de POST /songs colgada mientras intenta indexar, en vez de
# fallar rápido y devolver igualmente 201 (misma lección de Fase 8 con boto3).
_client = Client(settings.meilisearch_url, settings.meilisearch_api_key, timeout=5)


def ensure_index_exists() -> None:
    """Se llama UNA vez al arrancar la app (evento startup de FastAPI) o al
    principio de la sesión de tests - nunca perezosamente por request, mismo
    patrón que ensure_bucket_exists (Fase 8)."""
    try:
        _client.get_index(_INDEX_NAME)
    except MeilisearchApiError:
        # primaryKey="id" explícito: sin esto, Meilisearch intenta inferir la
        # primary key automáticamente y falla porque hay dos campos que
        # terminan en "id" (id, uploaded_by_id) - encontrado empíricamente
        # durante la implementación de esta fase, no una suposición.
        task_info = _client.create_index(_INDEX_NAME, {"primaryKey": "id"})
        _client.wait_for_task(task_info.task_uid, timeout_in_ms=_TASK_TIMEOUT_MS)

    index = _client.index(_INDEX_NAME)
    task_info = index.update_searchable_attributes(["title", "artist"])
    _client.wait_for_task(task_info.task_uid, timeout_in_ms=_TASK_TIMEOUT_MS)


def index_song(song: Song) -> None:
    """Best-effort, nunca propaga: si falla indexar, se loguea y no pasa
    nada más - la subida (POST /songs) ya respondió 201 con la canción
    reproducible, la búsqueda es una capa derivada, no el núcleo de la
    funcionalidad (ver docs/architecture.md, Fase 10).

    Defensa en profundidad: no confía ciegamente en que el llamador solo la
    invoque para canciones status="ready" - lo comprueba aquí también."""
    if song.status != "ready":
        logger.warning(
            "index_song llamada para la canción %s con status=%s (se esperaba "
            "'ready') - no se indexa",
            song.id,
            song.status,
        )
        return

    document = {
        "id": song.id,
        "title": song.title,
        "artist": song.artist,
        "uploaded_by_id": song.uploaded_by_id,
        "status": song.status,
        "duration_seconds": song.duration_seconds,
        "content_type": song.content_type,
        "file_size_bytes": song.file_size_bytes,
        "error_message": song.error_message,
        "created_at": song.created_at.isoformat(),
    }

    try:
        index = _client.index(_INDEX_NAME)
        task_info = index.add_documents([document])
        # Espera la task antes de volver: así la canción es buscable
        # inmediatamente después de que POST /songs responda 201, sin ventana
        # de carrera - Meilisearch no tiene un "refresh_interval" tipo
        # Elasticsearch, task.status == "succeeded" implica ya buscable.
        task = _client.wait_for_task(task_info.task_uid, timeout_in_ms=_TASK_TIMEOUT_MS)
        if task.status != "succeeded":
            raise RuntimeError(f"Meilisearch task {task.status}: {task.error}")
    except Exception:
        logger.exception("No se pudo indexar la canción %s en Meilisearch", song.id)


def search_songs(query: str, limit: int, offset: int) -> list[dict[str, Any]]:
    """A diferencia de index_song, esta función SÍ propaga la excepción si
    Meilisearch no responde - fallar en silencio devolviendo una lista vacía
    sería engañoso (parecería "sin resultados" en vez de "el buscador está
    caído"). El endpoint que la llama decide qué código HTTP devolver."""
    index = _client.index(_INDEX_NAME)
    results = index.search(query, {"limit": limit, "offset": offset})
    hits: list[dict[str, Any]] = results["hits"]
    return hits
