"""Reindexa en Meilisearch todas las canciones status="ready" que existan en
Postgres - red de seguridad manual para dos escenarios ya documentados:
canciones subidas antes de que existiera indexación (Fase 10) y el índice de
desarrollo vaciado o contaminado por accidente (ej. corriendo la suite de
tests sin el aislamiento de índice, ver docs/architecture.md). Añadir/
actualizar un documento en Meilisearch por id es idempotente, así que
reindexar una canción ya indexada no duplica nada - y sobrescribe cualquier
documento con datos obsoletos que tuviera ese mismo id.

Debe correr DENTRO del contenedor "app" (igual que app/cli/seed_catalog.py) -
DATABASE_URL/MEILISEARCH_URL dependen de los hostnames internos de
docker-compose.yml, no resolubles desde el host:

    docker compose exec app python -m app.cli.reindex_catalog
"""

import sys

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.session import SessionLocal
from app.models.song import Song
from app.services import search

_PROGRESS_EVERY = 50


def reindex_all(db: Session) -> tuple[int, int]:
    """Reindexa cada Song status="ready". Devuelve (reindexadas, fallidas).

    No comprueba la disponibilidad de Meilisearch de antemano - eso es
    responsabilidad de quien la llama (ver main()). Intenta TODAS las filas
    sin abortar a mitad: un puñado de filas con datos corruptos (ej. un
    `created_at` nulo por un insert manual) no debe poder impedir que se
    reindexe el resto de un catálogo sano - descartado a propósito un
    fail-fast basado en "N fallos seguidos" (hallazgo de la revisión: no
    distingue datos malos de Meilisearch caído, y podría abortar prematuro
    con un diagnóstico engañoso)."""
    songs = list(db.scalars(select(Song).where(Song.status == "ready")))
    ok = 0
    fail = 0

    for i, song in enumerate(songs, start=1):
        try:
            indexed = search.index_song(song)
        except Exception:
            # index_song ya no debería propagar nunca, pero esta llamada no
            # depende de esa garantía (defensa en profundidad, mismo hedge
            # que app/cli/seed_catalog.py) - también cubre errores fuera del
            # propio try/except interno de index_song, ej. construir el
            # documento a partir de una fila con datos inesperados.
            indexed = False

        if indexed:
            ok += 1
        else:
            fail += 1

        if i % _PROGRESS_EVERY == 0:
            print(f"... {i}/{len(songs)}", file=sys.stderr)

    return ok, fail


def main() -> None:
    # Comprobación de disponibilidad ANTES de tocar la base de datos o el
    # catálogo - si Meilisearch está caído, falla rápido con un mensaje claro
    # en vez de intentar cientos de filas a ~15s cada una (5s de timeout de
    # conexión + 10s de wait_for_task) solo para descubrirlo al final.
    if not search.is_healthy():
        print(
            "Meilisearch no está disponible - abortando antes de empezar.",
            file=sys.stderr,
        )
        sys.exit(1)

    db = SessionLocal()
    try:
        ok, fail = reindex_all(db)
        print(f"Reindexadas: {ok}, fallidas: {fail}")
        if fail:
            sys.exit(1)
    finally:
        db.close()


if __name__ == "__main__":  # pragma: no cover
    main()
