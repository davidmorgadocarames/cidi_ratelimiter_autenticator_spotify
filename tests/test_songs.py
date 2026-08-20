import os
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any, cast

import httpx
import pytest
from botocore.exceptions import ClientError
from fastapi import UploadFile
from fastapi.testclient import TestClient
from mypy_boto3_s3.client import S3Client
from sqlalchemy.orm import Session

from app.api.songs import _MAX_UPLOAD_BYTES, _read_upload_to_tempfile
from app.core.config import settings
from app.models.song import Song
from app.services import storage

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


def _upload(
    client: TestClient,
    access_token: str,
    audio_path: Path,
    title: str = "Test Song",
    artist: str = "Test Artist",
) -> httpx.Response:
    with audio_path.open("rb") as f:
        return client.post(
            "/songs",
            headers=_headers(access_token),
            data={"title": title, "artist": artist},
            files={"file": ("cancion.mp3", f, "audio/mpeg")},
        )


def test_upload_valid_audio_transcodes_and_stores_in_minio(
    client: TestClient, sample_audio_file: Path, s3_client: S3Client
) -> None:
    token = _register_and_login(client, "uploader@example.com")

    response = _upload(client, token, sample_audio_file)

    assert response.status_code == 201
    body = response.json()
    assert body["status"] == "ready"
    assert body["duration_seconds"] > 0
    assert body["title"] == "Test Song"
    assert body["artist"] == "Test Artist"

    song_id = body["id"]
    original_key = f"original/{song_id}/source"
    transcoded_key = f"transcoded/{song_id}.mp3"

    # Verificación real contra MinIO, no solo confiar en la respuesta HTTP.
    s3_client.head_object(Bucket=settings.s3_bucket_name, Key=original_key)
    s3_client.head_object(Bucket=settings.s3_bucket_name, Key=transcoded_key)

    # El objeto transcodificado se descarga y se vuelve a pasar por ffprobe
    # para confirmar que es audio válido de verdad, no solo que "algo" se subió.
    fd, downloaded_path = tempfile.mkstemp(suffix=".mp3")
    try:
        os.close(fd)
        s3_client.download_file(
            settings.s3_bucket_name, transcoded_key, downloaded_path
        )
        probe = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "json",
                downloaded_path,
            ],
            capture_output=True,
            check=True,
        )
        assert b'"duration"' in probe.stdout
    finally:
        Path(downloaded_path).unlink(missing_ok=True)


def test_upload_non_audio_file_rejected(client: TestClient) -> None:
    token = _register_and_login(client, "badupload@example.com")

    response = client.post(
        "/songs",
        headers=_headers(token),
        data={"title": "No es audio", "artist": "Nadie"},
        files={"file": ("nota.txt", b"esto no es un archivo de audio", "text/plain")},
    )

    assert response.status_code == 422

    listing = client.get("/songs", headers=_headers(token))
    assert listing.json() == []


def test_upload_oversized_file_rejected(client: TestClient) -> None:
    token = _register_and_login(client, "oversize@example.com")
    oversized = b"0" * (21 * 1024 * 1024)

    response = client.post(
        "/songs",
        headers=_headers(token),
        data={"title": "Enorme", "artist": "Nadie"},
        files={"file": ("grande.mp3", oversized, "audio/mpeg")},
    )

    assert response.status_code == 413


def test_upload_without_auth_rejected(
    sample_audio_file: Path, client: TestClient
) -> None:
    with sample_audio_file.open("rb") as f:
        response = client.post(
            "/songs",
            data={"title": "Sin auth", "artist": "Nadie"},
            files={"file": ("cancion.mp3", f, "audio/mpeg")},
        )
    assert response.status_code == 401


def test_catalog_is_public_across_users(
    client: TestClient, sample_audio_file: Path
) -> None:
    token_a = _register_and_login(client, "catalog-a@example.com")
    token_b = _register_and_login(client, "catalog-b@example.com")

    upload_response = _upload(client, token_a, sample_audio_file, title="Canción de A")
    song_id = upload_response.json()["id"]

    # B, que no subió nada, ve la canción de A tanto en la lista como en el detalle.
    listing = client.get("/songs", headers=_headers(token_b))
    assert any(song["id"] == song_id for song in listing.json())

    detail = client.get(f"/songs/{song_id}", headers=_headers(token_b))
    assert detail.status_code == 200
    assert detail.json()["title"] == "Canción de A"


def test_get_nonexistent_song_returns_404(client: TestClient) -> None:
    token = _register_and_login(client, "notfound@example.com")
    response = client.get("/songs/999999", headers=_headers(token))
    assert response.status_code == 404


def test_upload_covered_by_general_rate_limit_tier(
    client: TestClient, sample_audio_file: Path
) -> None:
    token = _register_and_login(client, "ratelimit-songs@example.com")
    response = _upload(client, token, sample_audio_file)
    assert response.headers["X-RateLimit-Limit"] == "60"


def test_upload_failure_mid_pipeline_marks_failed_and_cleans_up_orphan(
    client: TestClient,
    sample_audio_file: Path,
    s3_client: S3Client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """El original SÍ llega a subirse a MinIO antes de que falle la
    transcodificación (simulada) - la respuesta debe seguir siendo 201 con
    status="failed" (la fila existe, el fallo queda registrado), y el objeto
    huérfano debe limpiarse de MinIO, no quedar abandonado."""

    def _boom(_input_path: str, _output_path: str) -> None:
        raise RuntimeError("ffmpeg simulado explotando")

    monkeypatch.setattr("app.api.songs.transcode_to_mp3", _boom)

    token = _register_and_login(client, "failure@example.com")
    response = _upload(client, token, sample_audio_file)

    assert response.status_code == 201
    body = response.json()
    assert body["status"] == "failed"
    assert body["error_message"] == "No se pudo procesar el archivo de audio."

    original_key = f"original/{body['id']}/source"
    with pytest.raises(ClientError):
        s3_client.head_object(Bucket=settings.s3_bucket_name, Key=original_key)


def test_read_upload_to_tempfile_enforces_limit_via_chunked_read() -> None:
    """Prueba _read_upload_to_tempfile en aislamiento (sin pasar por HTTP/
    Content-Length) - confirma que el corte por chunks funciona por sí solo,
    no solo el atajo de Content-Length (que un cliente podría omitir/mentir)."""

    class _FakeFile:
        def __init__(self, total_bytes: int) -> None:
            self._remaining = total_bytes

        def read(self, chunk_size: int) -> bytes:
            if self._remaining <= 0:
                return b""
            take = min(chunk_size, self._remaining)
            self._remaining -= take
            return b"0" * take

    class _FakeUploadFile:
        def __init__(self, total_bytes: int) -> None:
            self.file: Any = _FakeFile(total_bytes)

    # cast: doble de prueba con duck-typing (solo necesita .file.read()), no
    # un UploadFile real - evitar construir un UploadFile de verdad con
    # decenas de MB en memoria solo para este test.
    fake_file = cast(UploadFile, _FakeUploadFile(_MAX_UPLOAD_BYTES + 1))
    with pytest.raises(Exception) as exc_info:
        _read_upload_to_tempfile(fake_file)
    assert getattr(exc_info.value, "status_code", None) == 413


def _create_song_directly(
    db_session: Session, uploaded_by_id: int, status_value: str
) -> Song:
    """Crea una fila Song sin pasar por el pipeline de subida - para probar
    los estados "processing"/"failed" del endpoint de streaming sin depender
    de mockear ffmpeg. NOT NULL en original_object_key/content_type/
    file_size_bytes, así que hay que rellenarlos igual con valores dummy."""
    song = Song(
        title="Canción de prueba",
        artist="Artista de prueba",
        uploaded_by_id=uploaded_by_id,
        status=status_value,
        original_object_key="original/999999/source",
        content_type="audio/mpeg",
        file_size_bytes=123,
    )
    db_session.add(song)
    db_session.commit()
    db_session.refresh(song)
    return song


def test_stream_url_returns_presigned_url_with_range_support(
    client: TestClient, sample_audio_file: Path
) -> None:
    token = _register_and_login(client, "streamer@example.com")
    upload_response = _upload(client, token, sample_audio_file)
    song_id = upload_response.json()["id"]

    stream_response = client.get(f"/songs/{song_id}/stream", headers=_headers(token))
    assert stream_response.status_code == 200
    body = stream_response.json()
    assert body["expires_in"] == 900
    url = body["url"]

    # Request HTTP real y directa contra la URL firmada (no vía TestClient,
    # no vía la app - directo contra MinIO), confirmando que Range Requests
    # funcionan de verdad, no solo que la URL se generó con buena pinta.
    full = httpx.get(url)
    assert full.status_code == 200
    total_bytes = len(full.content)

    partial = httpx.get(url, headers={"Range": "bytes=0-100"})
    assert partial.status_code == 206
    assert partial.headers["content-range"] == f"bytes 0-100/{total_bytes}"
    assert len(partial.content) == 101


def test_stream_url_rejects_processing_song(
    client: TestClient, db_session: Session
) -> None:
    token = _register_and_login(client, "streamprocessing@example.com")
    me = client.get("/auth/me", headers=_headers(token)).json()
    song = _create_song_directly(db_session, me["id"], "processing")

    response = client.get(f"/songs/{song.id}/stream", headers=_headers(token))
    assert response.status_code == 409


def test_stream_url_rejects_failed_song(
    client: TestClient, db_session: Session
) -> None:
    token = _register_and_login(client, "streamfailed@example.com")
    me = client.get("/auth/me", headers=_headers(token)).json()
    song = _create_song_directly(db_session, me["id"], "failed")

    response = client.get(f"/songs/{song.id}/stream", headers=_headers(token))
    assert response.status_code == 409


def test_stream_url_404_for_nonexistent_song(client: TestClient) -> None:
    token = _register_and_login(client, "streamnotfound@example.com")
    response = client.get("/songs/999999/stream", headers=_headers(token))
    assert response.status_code == 404


def test_stream_url_requires_auth(client: TestClient) -> None:
    response = client.get("/songs/1/stream")
    assert response.status_code == 401


def test_stream_url_available_to_any_authenticated_user(
    client: TestClient, sample_audio_file: Path
) -> None:
    token_a = _register_and_login(client, "stream-a@example.com")
    token_b = _register_and_login(client, "stream-b@example.com")

    upload_response = _upload(client, token_a, sample_audio_file)
    song_id = upload_response.json()["id"]

    # B, que no subió nada, puede pedir con éxito la URL de streaming de la
    # canción de A - mismo patrón de catálogo público que la lectura de
    # metadatos (Fase 8).
    response = client.get(f"/songs/{song_id}/stream", headers=_headers(token_b))
    assert response.status_code == 200


def test_presigned_url_expires_after_ttl(
    client: TestClient, sample_audio_file: Path
) -> None:
    """Expiración real, no simulada: el TTL de una firma SigV4 lo valida el
    reloj de MinIO server-side, no hay ningún timestamp en Postgres/Redis que
    manipular como en fases anteriores - la única forma de probarlo es dejar
    pasar tiempo de verdad."""
    token = _register_and_login(client, "expiry@example.com")
    upload_response = _upload(client, token, sample_audio_file)
    song_id = upload_response.json()["id"]
    transcoded_key = f"transcoded/{song_id}.mp3"

    short_lived_url = storage.generate_presigned_url(transcoded_key, expires_in=1)
    time.sleep(2)

    response = httpx.get(short_lived_url)
    assert response.status_code >= 400


def test_search_finds_uploaded_song_immediately_by_title(
    client: TestClient, sample_audio_file: Path
) -> None:
    """Sin sleep/retry: index_song espera la task de Meilisearch antes de que
    POST /songs responda 201, así que la canción ya es buscable en cuanto
    llega la respuesta - prueba real de que no hay ventana de carrera."""
    token = _register_and_login(client, "search-title@example.com")
    _upload(client, token, sample_audio_file, title="Bohemian Rhapsody", artist="Queen")

    response = client.get(
        "/songs/search", params={"q": "bohemian"}, headers=_headers(token)
    )
    assert response.status_code == 200
    results = response.json()
    assert any(r["title"] == "Bohemian Rhapsody" for r in results)


def test_search_finds_uploaded_song_by_artist(
    client: TestClient, sample_audio_file: Path
) -> None:
    token = _register_and_login(client, "search-artist@example.com")
    _upload(client, token, sample_audio_file, title="Otra canción", artist="Radiohead")

    response = client.get(
        "/songs/search", params={"q": "radiohead"}, headers=_headers(token)
    )
    assert response.status_code == 200
    results = response.json()
    assert any(r["artist"] == "Radiohead" for r in results)


def test_search_no_results_returns_empty_list(client: TestClient) -> None:
    token = _register_and_login(client, "search-empty@example.com")
    response = client.get(
        "/songs/search",
        params={"q": "algo-que-no-existe-en-ningun-titulo-xyz"},
        headers=_headers(token),
    )
    assert response.status_code == 200
    assert response.json() == []


def test_search_excludes_non_ready_songs(
    client: TestClient, db_session: Session
) -> None:
    """Solo se indexan las canciones status="ready" - una creada directo con
    status="processing"/"failed" nunca pasó por index_song, así que no
    debería aparecer en resultados de búsqueda aunque el título coincida."""
    token = _register_and_login(client, "search-notready@example.com")
    me = client.get("/auth/me", headers=_headers(token)).json()
    _create_song_directly(db_session, me["id"], "processing")

    response = client.get(
        "/songs/search", params={"q": "Canción de prueba"}, headers=_headers(token)
    )
    assert response.status_code == 200
    assert response.json() == []


def test_search_requires_auth(client: TestClient) -> None:
    response = client.get("/songs/search", params={"q": "algo"})
    assert response.status_code == 401


def test_list_songs_excludes_non_ready_songs(
    client: TestClient, db_session: Session, sample_audio_file: Path
) -> None:
    """Ajuste alineado de la Fase 10: GET /songs ya no lista canciones
    "processing"/"failed" - antes las incluía sin filtrar."""
    token = _register_and_login(client, "list-notready@example.com")
    me = client.get("/auth/me", headers=_headers(token)).json()
    not_ready = _create_song_directly(db_session, me["id"], "processing")
    ready_upload = _upload(client, token, sample_audio_file, title="Sí listada")
    ready_id = ready_upload.json()["id"]

    listing = client.get("/songs", headers=_headers(token)).json()
    listed_ids = {song["id"] for song in listing}
    assert ready_id in listed_ids
    assert not_ready.id not in listed_ids


def test_upload_indexing_failure_does_not_block_upload(
    client: TestClient, sample_audio_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fallo de indexación en Meilisearch (simulado) no debe bloquear la
    subida - MinIO/ffmpeg ya tuvieron éxito, la canción sigue siendo 201/
    status="ready" (best-effort, ver app/services/search.py)."""

    def _boom(_song: Song) -> None:
        raise RuntimeError("Meilisearch simulado explotando")

    monkeypatch.setattr("app.api.songs.search.index_song", _boom)

    token = _register_and_login(client, "index-failure@example.com")
    response = _upload(client, token, sample_audio_file)

    assert response.status_code == 201
    assert response.json()["status"] == "ready"


def test_search_returns_503_when_meilisearch_unreachable(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Meilisearch caído al BUSCAR (no al indexar) no debe devolver una lista
    vacía engañosa - search_songs propaga la excepción, el endpoint responde
    503 explícito."""

    def _boom(_query: str, limit: int, offset: int) -> list[dict[str, object]]:
        raise RuntimeError("Meilisearch inalcanzable simulado")

    monkeypatch.setattr("app.api.songs.search.search_songs", _boom)

    token = _register_and_login(client, "search-down@example.com")
    response = client.get(
        "/songs/search", params={"q": "algo"}, headers=_headers(token)
    )
    assert response.status_code == 503


def test_search_rejects_invalid_limit_with_422_not_503(client: TestClient) -> None:
    """Un limit/offset inválido (ej. negativo) es un error de validación de
    entrada (422), no una caída del buscador (503) - hallazgo de la revisión
    post-implementación: sin Query(ge=..., le=...), este valor llegaba crudo a
    Meilisearch, que respondía 400, y el except genérico del endpoint lo
    convertía en un 503 engañoso."""
    token = _register_and_login(client, "search-invalid-limit@example.com")
    response = client.get(
        "/songs/search",
        params={"q": "algo", "limit": -1},
        headers=_headers(token),
    )
    assert response.status_code == 422
