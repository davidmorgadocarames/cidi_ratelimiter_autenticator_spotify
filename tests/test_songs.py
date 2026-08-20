import os
import subprocess
import tempfile
from pathlib import Path
from typing import Any, cast

import httpx
import pytest
from botocore.exceptions import ClientError
from fastapi import UploadFile
from fastapi.testclient import TestClient
from mypy_boto3_s3.client import S3Client

from app.api.songs import _MAX_UPLOAD_BYTES, _read_upload_to_tempfile
from app.core.config import settings

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
