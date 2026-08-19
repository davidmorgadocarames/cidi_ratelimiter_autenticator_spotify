import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from app.core.config import settings
from app.db.session import Base, get_db
from app.main import app
from app.models import user  # noqa: F401 - registra los modelos en Base.metadata

test_engine = create_engine(settings.test_database_url)
TestSessionLocal = sessionmaker(bind=test_engine, autoflush=False, autocommit=False)


@pytest.fixture(scope="session", autouse=True)
def _create_tables():
    Base.metadata.create_all(bind=test_engine)
    yield
    Base.metadata.drop_all(bind=test_engine)


@pytest.fixture(autouse=True)
def _clean_tables():
    with test_engine.begin() as conn:
        conn.execute(text("TRUNCATE users, refresh_tokens RESTART IDENTITY CASCADE"))
    yield


def _override_get_db():
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
def db_session():
    """Sesión directa a la DB de test, para manipular filas desde los tests
    (ej. forzar la expiración de un refresh token sin esperar 7 días reales)."""
    db = TestSessionLocal()
    try:
        yield db
    finally:
        db.close()
