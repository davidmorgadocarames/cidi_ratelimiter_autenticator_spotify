"""Siembra el catálogo con canciones "reales" (título/artista de un dataset de
Kaggle) sin infringir copyright de audio: genera UN tono sintético con ffmpeg,
lo sube una única vez a MinIO, y todas las filas Song sembradas apuntan a esa
misma key compartida.

Requiere que app/cli/catalog_server.py ya esté corriendo (sirve el catálogo
por HTTP, ver ese módulo) y un usuario ya registrado (--user-email) - nunca
crea uno falso.

Uso (dentro del contenedor "app", ver docs/architecture.md):

    python -m app.cli.seed_catalog --user-email dev@example.com --limit 50
"""

import argparse
import contextlib
import json
import os
import subprocess
import sys
import tempfile
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.session import SessionLocal
from app.models.song import Song
from app.models.user import User
from app.services import search, storage

# Key fija, compartida por TODAS las canciones sembradas - nunca una por
# canción, a diferencia de las subidas reales de POST /songs (app/api/songs.py).
_SYNTHETIC_AUDIO_KEY = "seed/kaggle-synthetic-tone.mp3"
_CONTENT_TYPE = "audio/mpeg"

# Tope duro de --limit: sin esto, un valor grande (el dataset de Kaggle tiene
# ~114k filas) dispara un commit + una llamada síncrona a Meilisearch
# (index_song -> wait_for_task, timeout 10s) POR FILA, potencialmente varios
# minutos sin ningún aviso ni forma de cancelar limpiamente.
_MAX_LIMIT = 1000

# String(255) en el modelo Song (app/models/song.py) - truncar aquí evita que
# un título/artista largo del dataset reviente el INSERT a mitad del bucle de
# seed_tracks.
_MAX_FIELD_LENGTH = 255

_PAGE_SIZE = 100
_PROGRESS_EVERY = 10


@dataclass
class TrackRecord:
    title: str
    artist: str


def fetch_tracks(source_url: str, limit: int) -> list[TrackRecord]:
    """Pagina contra --source-url hasta acumular `limit` tracks o recibir una
    página vacía. Propaga cualquier excepción de red tal cual - la traduce a
    un mensaje accionable quien la llama (main), no esta función interna."""
    tracks: list[TrackRecord] = []
    offset = 0
    while len(tracks) < limit:
        page_size = min(_PAGE_SIZE, limit - len(tracks))
        url = f"{source_url}?offset={offset}&limit={page_size}"
        with urllib.request.urlopen(url, timeout=10) as response:
            payload = json.loads(response.read())
        if not payload:
            break
        tracks.extend(
            TrackRecord(title=item["title"], artist=item["artist"]) for item in payload
        )
        offset += page_size
    return tracks


def ensure_synthetic_audio(
    tone_duration_seconds: float = 30.0,
) -> tuple[str, float, int]:
    """Genera un tono sintético con ffmpeg (mismo patrón que
    tests/conftest.py::sample_audio_file) y lo sube a MinIO bajo la key
    compartida. Devuelve (key, duration_seconds, file_size_bytes)."""
    storage.ensure_bucket_exists()

    fd, path_str = tempfile.mkstemp(suffix=".mp3")
    os.close(fd)
    path = Path(path_str)
    try:
        subprocess.run(
            [
                "ffmpeg",
                "-f",
                "lavfi",
                "-i",
                f"sine=frequency=440:duration={tone_duration_seconds}",
                "-y",
                str(path),
            ],
            capture_output=True,
            check=True,
        )
        storage.upload_file(_SYNTHETIC_AUDIO_KEY, str(path), _CONTENT_TYPE)
        file_size_bytes = path.stat().st_size
    finally:
        path.unlink(missing_ok=True)

    return _SYNTHETIC_AUDIO_KEY, tone_duration_seconds, file_size_bytes


def seed_tracks(
    db: Session,
    tracks: list[TrackRecord],
    user_id: int,
    audio_key: str,
    duration_seconds: float,
    file_size_bytes: int,
) -> tuple[int, int, int]:
    """Crea una fila Song por track nuevo (dedup por título+artista ya
    truncados). Devuelve (creados, omitidos, sin_indexar). `sin_indexar`
    cuenta cuántas de las creadas no quedaron buscables en Meilisearch -
    index_song ya es best-effort y nunca bloquea la creación de la fila, pero
    sin este contador el resumen final ("Creadas: N") mentiría por omisión si
    Meilisearch estuviera caído durante toda la tirada.

    Ante cualquier excepción a mitad del bucle, imprime a stderr el recuento
    parcial acumulado y hace rollback antes de re-lanzar - con commit por
    fila, un fallo a mitad no debe dejar al usuario sin saber cuánto se llegó
    a sembrar, ni la sesión en una transacción fallida a medias."""
    created = 0
    skipped = 0
    not_indexed = 0
    try:
        for i, track in enumerate(tracks, start=1):
            title = track.title[:_MAX_FIELD_LENGTH]
            artist = track.artist[:_MAX_FIELD_LENGTH]

            existing_id = db.scalar(
                select(Song.id).where(Song.title == title, Song.artist == artist)
            )
            if existing_id is not None:
                skipped += 1
                continue

            song = Song(
                title=title,
                artist=artist,
                uploaded_by_id=user_id,
                status="ready",
                original_object_key=audio_key,
                transcoded_object_key=audio_key,
                duration_seconds=duration_seconds,
                content_type=_CONTENT_TYPE,
                file_size_bytes=file_size_bytes,
            )
            db.add(song)
            db.commit()
            db.refresh(song)
            created += 1

            # index_song ya es best-effort y no debería propagar nunca, pero
            # esta llamada no depende de esa garantía (defensa en profundidad).
            indexed = False
            with contextlib.suppress(Exception):
                indexed = search.index_song(song)
            if not indexed:
                not_indexed += 1

            if i % _PROGRESS_EVERY == 0:
                print(f"... {i}/{len(tracks)}", file=sys.stderr)
    except Exception:
        db.rollback()
        print(
            f"Fallo tras crear {created}, omitir {skipped} - revisa el error de arriba",
            file=sys.stderr,
        )
        raise

    return created, skipped, not_indexed


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--user-email", required=True)
    parser.add_argument("--source-url", default="http://localhost:8899/tracks")
    parser.add_argument("--limit", type=int, default=50)
    parser.add_argument("--tone-duration", type=float, default=30.0)
    args = parser.parse_args(argv)

    if args.limit > _MAX_LIMIT:
        print(
            f"--limit {args.limit} supera el máximo permitido ({_MAX_LIMIT}). "
            "Corre varias tandas más pequeñas si necesitas sembrar más.",
            file=sys.stderr,
        )
        sys.exit(1)

    db = SessionLocal()
    try:
        user_id = db.scalar(select(User.id).where(User.email == args.user_email))
        if user_id is None:
            print(
                f"No existe ningún usuario registrado con el email "
                f"{args.user_email!r}.",
                file=sys.stderr,
            )
            sys.exit(1)

        try:
            # OSError cubre URLError/ConnectionError/TimeoutError (todas
            # subclases); ValueError cubre una respuesta que no sea JSON
            # válido (json.JSONDecodeError); KeyError cubre un item sin
            # "title"/"artist" - cualquiera de estos significa que
            # --source-url no apunta a un catalog_server sano.
            tracks = fetch_tracks(args.source_url, args.limit)
        except (OSError, ValueError, KeyError) as exc:
            print(
                f"No se pudo leer el catálogo de --source-url={args.source_url!r} "
                f"({exc}). ¿Está corriendo `python -m app.cli.catalog_server`?",
                file=sys.stderr,
            )
            sys.exit(1)

        try:
            audio_key, duration_seconds, file_size_bytes = ensure_synthetic_audio(
                args.tone_duration
            )
        except (OSError, subprocess.CalledProcessError) as exc:
            print(
                f"No se pudo generar/subir el audio sintético ({exc}). "
                "¿Está `ffmpeg` instalado? Este script está pensado para correr "
                'dentro del contenedor "app" (`docker compose exec app ...`).',
                file=sys.stderr,
            )
            sys.exit(1)

        created, skipped, not_indexed = seed_tracks(
            db, tracks, user_id, audio_key, duration_seconds, file_size_bytes
        )
        print(
            f"Creadas: {created}, omitidas (ya existían): {skipped}, "
            f"sin indexar en el buscador: {not_indexed}"
        )
    finally:
        db.close()


if __name__ == "__main__":  # pragma: no cover
    main()
