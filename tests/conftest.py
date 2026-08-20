import os
import subprocess
import tempfile
from collections.abc import Generator, Iterator
from pathlib import Path

import boto3
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
from app.models import song, user  # noqa: F401 - registra los modelos en Base.metadata
from app.services.search import _INDEX_NAME, ensure_index_exists
from app.services.storage import ensure_bucket_exists

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
    index = test_meilisearch.index(_INDEX_NAME)
    task_info = index.delete_all_documents()
    test_meilisearch.wait_for_task(task_info.task_uid)


@pytest.fixture(autouse=True)
def _clean_tables() -> Generator[None, None, None]:
    with test_engine.begin() as conn:
        conn.execute(
            text("TRUNCATE users, refresh_tokens, songs RESTART IDENTITY CASCADE")
        )
    yield


@pytest.fixture(autouse=True)
def _flush_redis() -> Generator[None, None, None]:
    """Aísla los buckets del rate limiter entre tests, mismo patrón que el
    TRUNCATE de Postgres de arriba. Ver docs/architecture.md para la limitación
    conocida (no compatible con pytest-xdist real, no usado en este proyecto)."""
    test_redis.flushdb()
    yield


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
