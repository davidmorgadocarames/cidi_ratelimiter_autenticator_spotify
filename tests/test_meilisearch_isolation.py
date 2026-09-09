from app.core.config import settings
from app.services import search


def test_search_module_uses_the_test_index() -> None:
    """Red de seguridad barata: si alguien borra la línea de override de
    tests/conftest.py sin darse cuenta, este test falla explícitamente en vez
    de dejar que la suite vuelva a vaciar el índice "songs" de desarrollo en
    silencio (ver docs/architecture.md)."""
    assert settings.meilisearch_test_index_name == search._INDEX_NAME
    assert settings.meilisearch_index_name != search._INDEX_NAME


def test_is_healthy_true_against_real_meilisearch() -> None:
    """Meilisearch real está arriba durante los tests (misma infraestructura
    que el resto de la suite) - app/cli/reindex_catalog.py depende de que
    esta comprobación funcione de verdad, no solo de que exista."""
    assert search.is_healthy() is True
