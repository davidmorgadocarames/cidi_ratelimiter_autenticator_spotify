import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.api.auth import router as auth_router
from app.api.songs import router as songs_router
from app.api.totp import router as totp_router
from app.api.users import router as users_router
from app.core.rate_limiter import RateLimitMiddleware
from app.services.search import ensure_index_exists
from app.services.storage import ensure_bucket_exists

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    # Una sola vez al arrancar, no perezosamente por request - evita el TOCTOU
    # de dos primeras-subidas concurrentes creando el bucket a la vez (ver
    # app/services/storage.py). Nota: TestClient(app) sin "with" (como usa la
    # fixture "client" de tests/conftest.py) no dispara este evento - la suite
    # de tests asegura el bucket con su propia fixture de sesión, no con esto.
    #
    # No debe bloquear ni tumbar el arranque de TODA la app si MinIO no está
    # disponible en ese instante (verificado empíricamente: sin este try/except
    # el arranque se queda colgado) - mismo principio de fail-open ya aplicado
    # al rate limiter con Redis en la Fase 6: el resto de la API (login, 2FA,
    # etc.) no depende de MinIO y debe poder seguir sirviendo tráfico. Solo
    # /songs se vería afectado, con errores en cada request, no con la app
    # entera caída.
    try:
        ensure_bucket_exists()
    except Exception:
        logger.exception(
            "No se pudo verificar/crear el bucket de MinIO al arrancar - "
            "/songs fallará hasta que MinIO esté disponible"
        )

    # Try/except PROPIO, no compartido con el de MinIO de arriba (hallazgo de
    # la revisión del plan): un fallo de Meilisearch debe loguearse con su
    # propio mensaje, no el genérico de MinIO. Mismo principio fail-open: el
    # resto de la API no depende de Meilisearch, solo /songs/search se vería
    # afectado (con 503, ver app/api/songs.py), no la app entera.
    try:
        ensure_index_exists()
    except Exception:
        logger.exception(
            "No se pudo verificar/crear el índice de Meilisearch al arrancar - "
            "/songs/search fallará hasta que Meilisearch esté disponible"
        )
    yield


app = FastAPI(title="CIDI Spotify Clone API", lifespan=lifespan)

app.add_middleware(RateLimitMiddleware)

app.include_router(auth_router)
app.include_router(users_router)
app.include_router(totp_router)
app.include_router(songs_router)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


# Montado al final: los routers de arriba tienen prioridad de ruteo sobre este
# catch-all de estáticos (UI mínima integrada, ver docs/architecture.md).
app.mount("/", StaticFiles(directory="app/static", html=True), name="static")
