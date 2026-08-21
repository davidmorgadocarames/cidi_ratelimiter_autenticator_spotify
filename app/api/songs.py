import logging
import os
import tempfile
from pathlib import Path

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Query,
    Request,
    UploadFile,
    status,
)
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.auth import get_current_user
from app.core.redis_client import redis_client
from app.db.session import get_db
from app.models.play import SongPlay
from app.models.song import Song
from app.models.user import User
from app.schemas.song import SongRead, SongStreamURL
from app.services import popular, search, storage
from app.services.transcode import (
    TranscodeError,
    probe_duration_seconds,
    transcode_to_mp3,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/songs", tags=["songs"])

# 20 MB: margen para un mp3 de varios minutos en bitrate alto sin permitir
# subidas que agoten disco/memoria sin límite. Starlette/FastAPI no imponen
# ningún límite de tamaño por sí solos - hay que aplicarlo explícitamente.
# LIMITACIÓN CONOCIDA (no resuelta en esta fase, documentada en
# docs/architecture.md): con UploadFile de alto nivel, Starlette ya parsea el
# multipart completo y lo vuelca a un SpooledTemporaryFile propio (a disco a
# partir de ~1MB) ANTES de que este código se ejecute. Este límite acota lo
# que la propia app procesa/persiste/transcodifica y sube a MinIO, pero no
# evita que Starlette reciba y spoolee a disco el cuerpo completo de una
# subida enorme antes de este punto. Una defensa completa exigiría leer el
# stream crudo de la request a mano - desproporcionado para el alcance de
# esta fase.
_MAX_UPLOAD_BYTES = 20 * 1024 * 1024
_CHUNK_SIZE = 1024 * 1024

_GENERIC_FAILURE_MESSAGE = "No se pudo procesar el archivo de audio."

# 15 minutos: generoso para escuchar/rebobinar una canción de pocos minutos
# sin que la URL expire a mitad, pero acota razonablemente la ventana de
# exposición si la URL firmada se filtrara (queda en logs de red, historial
# del navegador, etc. - una URL firmada (SigV2 en la práctica, ver
# app/services/storage.py) lleva la autorización EN la propia URL, así que
# cualquiera que la consiga puede reproducir el audio sin sesión durante ese
# tiempo, ver docs/architecture.md).
_STREAM_URL_EXPIRES_IN_SECONDS = 900


def _reject_if_content_length_too_large(request: Request) -> None:
    content_length = request.headers.get("content-length")
    if content_length is None:
        return
    try:
        declared_size = int(content_length)
    except ValueError:
        # Header malformado: se ignora aquí, la lectura en chunks de abajo
        # sigue siendo la defensa real (esto es solo un atajo barato).
        return
    if declared_size > _MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail="El archivo supera el tamaño máximo permitido (20 MB)",
        )


def _read_upload_to_tempfile(file: UploadFile) -> tuple[str, int]:
    """Copia el UploadFile a un temporal propio en chunks, cortando en 413 si
    supera el límite. El nombre del temporal lo genera el SO (tempfile) - NUNCA
    se deriva de file.filename (dato controlado por el cliente)."""
    fd, path = tempfile.mkstemp(suffix=".upload")
    total = 0
    try:
        with os.fdopen(fd, "wb") as out:
            while True:
                chunk = file.file.read(_CHUNK_SIZE)
                if not chunk:
                    break
                total += len(chunk)
                if total > _MAX_UPLOAD_BYTES:
                    raise HTTPException(
                        status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                        detail="El archivo supera el tamaño máximo permitido (20 MB)",
                    )
                out.write(chunk)
    except Exception:
        Path(path).unlink(missing_ok=True)
        raise
    return path, total


@router.post("", response_model=SongRead, status_code=status.HTTP_201_CREATED)
def upload_song(
    request: Request,
    title: str = Form(...),
    artist: str = Form(...),
    file: UploadFile = File(...),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Song:
    _reject_if_content_length_too_large(request)
    original_path, file_size = _read_upload_to_tempfile(file)
    transcoded_path: str | None = None

    try:
        try:
            duration = probe_duration_seconds(original_path)
        except TranscodeError as exc:
            logger.warning("Archivo de audio inválido en /songs: %s", exc)
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Archivo de audio inválido",
            ) from exc

        song = Song(
            title=title,
            artist=artist,
            uploaded_by_id=current_user.id,
            status="processing",
            # Key fija, nunca derivada de file.filename - ver app/services/storage.py.
            original_object_key="",
            content_type=file.content_type or "application/octet-stream",
            file_size_bytes=file_size,
        )
        db.add(song)
        # flush (no commit): asigna song.id sin cerrar la transacción, así se
        # puede construir la key real y guardar todo en un único commit -
        # evita el round-trip extra y la ventana en la que otra transacción
        # podría leer la fila con original_object_key="" todavía.
        db.flush()

        original_key = f"original/{song.id}/source"
        song.original_object_key = original_key
        db.commit()
        db.refresh(song)

        uploaded_keys: list[str] = []
        try:
            storage.upload_file(original_key, original_path, song.content_type)
            uploaded_keys.append(original_key)

            fd, transcoded_path = tempfile.mkstemp(suffix=".mp3")
            os.close(fd)
            transcode_to_mp3(original_path, transcoded_path)

            transcoded_key = f"transcoded/{song.id}.mp3"
            storage.upload_file(transcoded_key, transcoded_path, "audio/mpeg")
            uploaded_keys.append(transcoded_key)

            song.status = "ready"
            song.duration_seconds = duration
            song.transcoded_object_key = transcoded_key
            db.commit()
            db.refresh(song)
        except Exception:
            logger.exception("Fallo procesando la canción %s", song.id)
            # Limpieza best-effort de lo que sí llegó a subirse a MinIO antes
            # del fallo - sin esto quedarían objetos huérfanos en el bucket
            # (encontrado en la revisión post-implementación: delete_object
            # existía pero nunca se llamaba).
            for key in uploaded_keys:
                storage.delete_object(key)
            song.status = "failed"
            song.error_message = _GENERIC_FAILURE_MESSAGE
            db.commit()
            db.refresh(song)

        # FUERA del try/except de arriba, a propósito (hallazgo de la revisión
        # del plan): ese try/except es el manejo BLOQUEANTE de MinIO/ffmpeg
        # (marca "failed" y borra objetos si algo de eso revienta). Indexar en
        # Meilisearch es best-effort (ver app/services/search.py) - un fallo
        # ahí no debe poder marcar "failed" ni borrar objetos de una canción
        # que SÍ es reproducible. Solo se llama si terminó "ready".
        #
        # try/except propio aquí, en defensa en profundidad (mismo principio
        # que index_song no confiando ciegamente en su llamador): index_song
        # ya nunca propaga por diseño, pero esta subida no debe depender de
        # que esa garantía se mantenga siempre (ni de que un test doble no la
        # reproduzca fielmente).
        if song.status == "ready":
            try:
                search.index_song(song)
            except Exception:
                logger.exception(
                    "search.index_song lanzó una excepción inesperada para la "
                    "canción %s (no debería propagar) - se ignora, la subida "
                    "sigue siendo válida",
                    song.id,
                )

        return song
    finally:
        Path(original_path).unlink(missing_ok=True)
        if transcoded_path is not None:
            Path(transcoded_path).unlink(missing_ok=True)


@router.get("", response_model=list[SongRead])
def list_songs(
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[Song]:
    # Solo "ready": ajuste alineado con la Fase 10 (hasta ahora listaba TODAS
    # las canciones sin filtrar, incluyendo "processing"/"failed", que no son
    # reproducibles) - sin esto, este listado mostraría canciones que
    # GET /songs/search nunca podría encontrar (solo se indexan las "ready"),
    # una discrepancia confusa entre las dos vistas del mismo catálogo.
    return list(
        db.scalars(
            select(Song)
            .where(Song.status == "ready")
            .order_by(Song.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
    )


@router.get("/search", response_model=list[SongRead])
def search_songs_endpoint(
    q: str,
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
    current_user: User = Depends(get_current_user),
) -> list[SongRead]:
    """Registrado ANTES de /{song_id} a propósito, no por casualidad: FastAPI/
    Starlette resuelve rutas en el orden en que se registran, y /{song_id} es
    un regex de segmento genérico - la validación de que song_id sea int
    ocurre DESPUÉS de que la ruta ya haya hecho match, no como parte del
    patrón de la URL. Si /{song_id} se registrara antes, una request a
    /songs/search matchearía esa ruta con song_id="search" y fallaría con 422
    en vez de llegar aquí.

    limit/offset acotados con Query(ge=..., le=...) - sin esto, un valor
    negativo llegaría crudo a Meilisearch, que respondería 400, y este except
    lo convertiría en un 503 engañoso ("buscador caído") en vez de un 422 de
    validación de entrada (hallazgo de la revisión post-implementación,
    confirmado por los tres revisores)."""
    try:
        hits = search.search_songs(q, limit=limit, offset=offset)
        return [SongRead(**hit) for hit in hits]
    except Exception as exc:
        logger.exception("Meilisearch no respondió a una búsqueda")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="El buscador no está disponible en este momento",
        ) from exc


@router.get("/popular", response_model=list[SongRead])
def get_popular_songs(
    limit: int = Query(20, ge=1, le=100),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[Song]:
    """Registrado ANTES de /{song_id}, mismo motivo que /search: sin esto,
    /songs/popular matchearía con song_id="popular" y fallaría con 422.

    Sin personalización a propósito - "popular" es la misma lista para
    cualquier usuario, a diferencia de GET /users/me/recommendations (Fase
    11). El cálculo pesado ocurre a lo sumo una vez cada 5 min (cache-aside
    en app/services/popular.py), no en cada request."""
    song_ids = popular.get_popular_song_ids(db, redis_client, limit)
    if not song_ids:
        return []

    songs = db.scalars(
        select(Song).where(Song.id.in_(song_ids), Song.status == "ready")
    ).all()
    songs_by_id = {song.id: song for song in songs}
    # Preserva el orden del caché (el ranking), no el orden natural de la
    # consulta SQL - un id colgante o ya no "ready" se omite en vez de romper
    # la respuesta (puede devolver menos de `limit` si eso ocurre).
    return [songs_by_id[song_id] for song_id in song_ids if song_id in songs_by_id]


@router.get("/{song_id}", response_model=SongRead)
def get_song(
    song_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Song:
    song = db.get(Song, song_id)
    if song is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Canción no encontrada"
        )
    return song


@router.get("/{song_id}/stream", response_model=SongStreamURL)
def get_song_stream_url(
    song_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> SongStreamURL:
    """Control plane: solo devuelve una URL firmada, nunca los bytes de audio
    (data plane, servidos directo por MinIO - ver docs/architecture.md). El
    cliente real (un <audio src>, curl, etc.) hace la request de streaming
    contra esa URL, no contra este endpoint - JSON, no un 302 directo, porque
    este endpoint SÍ exige Authorization: Bearer y un <audio> nativo no puede
    mandar ese header; la URL firmada, en cambio, ya lleva su propia
    autorización embebida y no necesita ninguno."""
    song = db.get(Song, song_id)
    if song is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Canción no encontrada"
        )
    if song.status != "ready":
        # Whitelist ("!= ready"), no blacklist ("== processing or == failed"):
        # "status" es un str libre a nivel de columna, sin CHECK/enum, y
        # "pending" está reservado para cuando exista una cola real (Fase 11)
        # - con una blacklist, un valor futuro no contemplado caería
        # silenciosamente en el chequeo de transcoded_object_key de abajo en
        # vez de dar el 409 correcto (hallazgo de la revisión post-implementación).
        detail = (
            "Todavía se está procesando"
            if song.status == "processing"
            else "El procesamiento de esta canción falló"
        )
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=detail)
    if song.transcoded_object_key is None:
        # No debería poder pasar nunca en la práctica (status="ready" siempre
        # implica transcoded_object_key ya fijado, ver upload_song arriba),
        # pero el campo es str | None a nivel de tipo y el guard de status no
        # lo estrecha para mypy --strict - este chequeo explícito satisface
        # el type-checker Y es una defensa real, no solo cosmética.
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Error interno: la canción no tiene un archivo transcodificado",
        )

    url = storage.generate_presigned_url(
        song.transcoded_object_key, expires_in=_STREAM_URL_EXPIRES_IN_SECONDS
    )

    # Best-effort, nunca bloquea la respuesta de streaming (Fase 11, hallazgo
    # de la revisión del plan): un fallo específico de este insert (deadlock,
    # serialization failure) no debe degradar una función núcleo que hoy es
    # puramente de lectura, solo porque falló una señal de analítica. Registra
    # "el usuario pidió esta URL", no "escuchó la canción completa" - no hay
    # UI de reproducción en este proyecto (Fase 9), es la señal más fiel
    # disponible.
    try:
        db.add(SongPlay(user_id=current_user.id, song_id=song.id))
        db.commit()
    except Exception:
        db.rollback()
        logger.exception(
            "No se pudo registrar el play de la canción %s para el usuario %s",
            song.id,
            current_user.id,
        )

    return SongStreamURL(url=url, expires_in=_STREAM_URL_EXPIRES_IN_SECONDS)
