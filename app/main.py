from fastapi import FastAPI

from app.api.auth import router as auth_router

app = FastAPI(title="CIDI Spotify Clone API")

app.include_router(auth_router)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
