from collections.abc import Generator, Iterator

import pytest
import redis as redis_sync
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import settings
from app.db.session import Base, get_db
from app.main import app
from app.models import user  # noqa: F401 - registra los modelos en Base.metadata

test_engine = create_engine(settings.test_database_url)
TestSessionLocal = sessionmaker(bind=test_engine, autoflush=False, autocommit=False)

# Cliente Redis para tests (limpiar buckets, manipular last_refill
# directamente) - conexión separada de la que usa el middleware, aunque
# ambas son síncronas (ver app/core/rate_limiter.py sobre por qué el
# middleware no usa redis.asyncio).
test_redis = redis_sync.Redis.from_url(settings.redis_url, decode_responses=True)


@pytest.fixture(scope="session", autouse=True)
def _create_tables() -> Generator[None, None, None]:
    Base.metadata.create_all(bind=test_engine)
    yield
    Base.metadata.drop_all(bind=test_engine)


@pytest.fixture(autouse=True)
def _clean_tables() -> Generator[None, None, None]:
    with test_engine.begin() as conn:
        conn.execute(text("TRUNCATE users, refresh_tokens RESTART IDENTITY CASCADE"))
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
