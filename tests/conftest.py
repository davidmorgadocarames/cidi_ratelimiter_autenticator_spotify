import email
import email.policy
import os
import subprocess
import tempfile
from collections.abc import Generator, Iterator
from email.message import EmailMessage
from pathlib import Path
from typing import Any, cast

import boto3
import httpx
import pytest
import redis as redis_sync
from fastapi.testclient import TestClient
from meilisearch import Client as MeilisearchClient
from mypy_boto3_s3.client import S3Client
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import settings
from app.db.session import Base, get_db
from app.main import app
from app.models import (  # noqa: F401 - registra los modelos en Base.metadata
    play,
    song,
    user,
)
from app.services import search
from app.services.search import ensure_index_exists
from app.services.storage import ensure_bucket_exists

# Aísla el índice de Meilisearch que usan los tests del que usa la app de desarrollo
# real - sin esto, _clean_index (abajo) vacía el índice "songs" compartido con
# cualquier `docker compose up` corriendo en paralelo (incidente real: verificando
# el seed de Kaggle, correr la suite borró la búsqueda de canciones reales
# sembradas, ver docs/architecture.md). search.py resuelve _INDEX_NAME como global
# de módulo en cada llamada (index_song/search_songs/ensure_index_exists), así que
# reasignarlo aquí, UNA vez al importar conftest.py (antes de que corra cualquier
# fixture o request de TestClient), redirige también las llamadas INDIRECTAS que
# disparan los endpoints reales bajo TestClient, no solo las de este archivo.
search._INDEX_NAME = settings.meilisearch_test_index_name

test_engine = create_engine(settings.test_database_url)
TestSessionLocal = sessionmaker(bind=test_engine, autoflush=False, autocommit=False)

# Cliente Redis para tests (limpiar buckets, manipular last_refill
# directamente) - conexión separada de la que usa el middleware, aunque
# ambas son síncronas (ver app/core/rate_limiter.py sobre por qué el
# middleware no usa redis.asyncio).
test_redis = redis_sync.Redis.from_url(settings.redis_url, decode_responses=True)

# Cliente boto3 de test para manipular/verificar objetos en MinIO directamente
# (mismo patrón que test_redis: conexión separada de la que usa la app).
test_s3: S3Client = boto3.client(
    "s3",
    endpoint_url=settings.s3_endpoint_url,
    aws_access_key_id=settings.s3_access_key,
    aws_secret_access_key=settings.s3_secret_key,
)

# Cliente Meilisearch de test para manipular/verificar el índice directamente
# (mismo patrón que test_redis/test_s3: conexión separada de la que usa la app).
test_meilisearch = MeilisearchClient(
    settings.meilisearch_url, settings.meilisearch_api_key, timeout=5
)


@pytest.fixture(scope="session", autouse=True)
def _create_tables() -> Generator[None, None, None]:
    Base.metadata.create_all(bind=test_engine)
    yield
    Base.metadata.drop_all(bind=test_engine)


@pytest.fixture(scope="session", autouse=True)
def _ensure_test_bucket() -> None:
    # Una sola vez por sesión de tests, no perezosamente (mismo motivo que en
    # app/main.py: evita el TOCTOU de crear el bucket bajo concurrencia). No
    # depende del ciclo de vida de TestClient/lifespan - la fixture "client"
    # de abajo instancia TestClient(app) sin "with", así que el evento
    # startup de la app real NUNCA se dispara en tests.
    ensure_bucket_exists()


@pytest.fixture(autouse=True)
def _clean_bucket() -> Generator[None, None, None]:
    """Vacía el bucket de MinIO entre tests, mismo patrón que _flush_redis."""
    yield
    objects = test_s3.list_objects_v2(Bucket=settings.s3_bucket_name).get(
        "Contents", []
    )
    if objects:
        test_s3.delete_objects(
            Bucket=settings.s3_bucket_name,
            Delete={"Objects": [{"Key": obj["Key"]} for obj in objects]},
        )


@pytest.fixture(scope="session", autouse=True)
def _ensure_test_index() -> None:
    # Una sola vez por sesión, mismo motivo que _ensure_test_bucket - no
    # depende del lifespan de la app real, que no se dispara en tests.
    ensure_index_exists()


@pytest.fixture(autouse=True)
def _clean_index() -> Generator[None, None, None]:
    """Vacía el índice de Meilisearch entre tests, mismo patrón que
    _clean_bucket/_flush_redis. Espera la task antes de continuar, para que
    el índice quede realmente vacío antes de que empiece el siguiente test."""
    yield
    index = test_meilisearch.index(settings.meilisearch_test_index_name)
    task_info = index.delete_all_documents()
    test_meilisearch.wait_for_task(task_info.task_uid)


@pytest.fixture(autouse=True)
def _clean_tables() -> Generator[None, None, None]:
    with test_engine.begin() as conn:
        conn.execute(
            text(
                "TRUNCATE users, refresh_tokens, songs, song_plays "
                "RESTART IDENTITY CASCADE"
            )
        )
    yield


@pytest.fixture(autouse=True)
def _flush_redis() -> Generator[None, None, None]:
    """Aísla los buckets del rate limiter entre tests, mismo patrón que el
    TRUNCATE de Postgres de arriba. Ver docs/architecture.md para la limitación
    conocida (no compatible con pytest-xdist real, no usado en este proyecto)."""
    test_redis.flushdb()
    yield


@pytest.fixture(autouse=True)
def _flush_mailhog() -> Generator[None, None, None]:
    """Vacía los correos capturados por Mailhog entre tests, mismo patrón que
    _clean_bucket/_clean_index. DELETE /api/v1/messages (no hay equivalente en
    v2) - verificado empíricamente contra el contenedor real."""
    yield
    httpx.delete(f"{settings.mailhog_api_url}/api/v1/messages", timeout=5)


def mark_email_verified(email: str) -> None:
    """Utilidad para los helpers _register_and_login ya existentes en otros
    archivos de test (test_songs.py, test_totp.py, etc.) - saltan el flujo
    real de verificación de email porque no es lo que están probando. Update
    directo (no pasa por Session/ORM) porque se llama desde fuera del ciclo
    de vida normal de un test con fixtures inyectadas."""
    with test_engine.begin() as conn:
        conn.execute(
            text("UPDATE users SET email_verified = true WHERE email = :email"),
            {"email": email},
        )


def get_mailhog_messages() -> list[dict[str, Any]]:
    """Lista los correos capturados por Mailhog vía su API real (GET
    /api/v2/messages) - usado por tests/test_email_verification.py para
    confirmar que un email se envió de verdad, sin mockear smtplib."""
    response = httpx.get(f"{settings.mailhog_api_url}/api/v2/messages", timeout=5)
    response.raise_for_status()
    items: list[dict[str, Any]] = response.json()["items"]
    return items


def decode_mailhog_body(message: dict[str, Any]) -> str:
    """El campo "Body" de la API v2 viene tal cual salió por SMTP, es decir
    todavía codificado quoted-printable (líneas cortadas con "=\\r\\n", "="
    escapado como "=3D") - encontrado empíricamente probando contra un
    contenedor real: un regex ingenuo sobre "Body" corta el token a mitad de
    línea. "Raw.Data" trae el mensaje MIME completo tal cual, que el propio
    parser de email de la stdlib sabe decodificar de verdad."""
    raw = message["Raw"]["Data"]
    # email.message_from_string() está tipado en typeshed para devolver el
    # Message genérico de compat32 aunque se le pase policy=default en
    # runtime (donde sí devuelve un EmailMessage real, con get_content()) -
    # cast explícito, no un # type: ignore genérico.
    parsed = cast(
        EmailMessage, email.message_from_string(raw, policy=email.policy.default)
    )
    content: str = parsed.get_content()
    return content


def _override_get_db() -> Iterator[Session]:
    db = TestSessionLocal()
    try:
        yield db
    finally:
        db.close()


app.dependency_overrides[get_db] = _override_get_db


@pytest.fixture()
def client() -> TestClient:
    return TestClient(app)


@pytest.fixture()
def db_session() -> Iterator[Session]:
    """Sesión directa a la DB de test, para manipular filas desde los tests
    (ej. forzar la expiración de un refresh token sin esperar 7 días reales)."""
    db = TestSessionLocal()
    try:
        yield db
    finally:
        db.close()


@pytest.fixture()
def redis_client() -> redis_sync.Redis:
    """Cliente Redis directo, para manipular buckets desde los tests (ej.
    forzar last_refill al pasado para simular refill sin esperar en tiempo
    real, mismo patrón que forzar expires_at en db_session)."""
    return test_redis


@pytest.fixture()
def s3_client() -> S3Client:
    """Cliente boto3 directo, para verificar objetos subidos a MinIO desde
    los tests (head_object, download_file para re-validar con ffprobe)."""
    return test_s3


@pytest.fixture()
def meilisearch_client() -> MeilisearchClient:
    """Cliente Meilisearch directo, para inspeccionar el índice desde los
    tests (ej. forzar un documento con status distinto de "ready" para
    probar el filtro de index_song sin pasar por el pipeline completo)."""
    return test_meilisearch


@pytest.fixture()
def test_session_factory() -> sessionmaker[Session]:
    """La fábrica de sesiones de test, para monkeypatchear módulos que abren
    su propia Session fuera del ciclo de vida de FastAPI (ej.
    app/cli/seed_catalog.py, que usa app.db.session.SessionLocal directamente
    porque es un script, no un endpoint) - mismo motivo que
    app.dependency_overrides[get_db] arriba, pero para código que no pasa por
    Depends(get_db)."""
    return TestSessionLocal


@pytest.fixture()
def sample_audio_file() -> Iterator[Path]:
    """Genera un archivo de audio real y minúsculo con ffmpeg (un tono de 1s)
    - nada de fixtures binarias commiteadas al repo."""
    fd, path_str = tempfile.mkstemp(suffix=".mp3")
    os.close(fd)
    path = Path(path_str)
    subprocess.run(
        [
            "ffmpeg",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=1",
            "-y",
            str(path),
        ],
        capture_output=True,
        check=True,
    )
    try:
        yield path
    finally:
        path.unlink(missing_ok=True)
