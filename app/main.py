from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.api.auth import router as auth_router
from app.api.totp import router as totp_router
from app.api.users import router as users_router
from app.core.rate_limiter import RateLimitMiddleware

app = FastAPI(title="CIDI Spotify Clone API")

app.add_middleware(RateLimitMiddleware)

app.include_router(auth_router)
app.include_router(users_router)
app.include_router(totp_router)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


# Montado al final: los routers de arriba tienen prioridad de ruteo sobre este
# catch-all de estáticos (UI mínima integrada, ver docs/architecture.md).
app.mount("/", StaticFiles(directory="app/static", html=True), name="static")
