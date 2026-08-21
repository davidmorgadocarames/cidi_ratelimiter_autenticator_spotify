from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.auth import get_current_user
from app.api.totp import (
    check_totp_lockout,
    lock_user_for_totp_check,
    register_totp_failure,
    register_totp_success,
)
from app.core.redis_client import redis_client
from app.core.security import (
    TOTPDecryptionError,
    decrypt_totp_secret,
    verify_password,
    verify_totp_code,
)
from app.db.session import get_db
from app.models.song import Song
from app.models.user import User
from app.schemas.song import SongRead
from app.schemas.user import PremiumActivateRequest, UserRead
from app.services.recommendations import get_recommendations

router = APIRouter(prefix="/users", tags=["users"])


@router.post("/me/premium/activate", response_model=UserRead)
def activate_premium(
    payload: PremiumActivateRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> User:
    if not current_user.totp_enabled:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Configura 2FA antes de activar premium",
        )

    locked_user = lock_user_for_totp_check(db, current_user.id)

    # Re-chequeo sobre locked_user (no current_user): mypy no puede inferir que
    # `totp_enabled` en current_user implica secreto no-nulo en esta fila recién
    # re-leída bajo lock; además es una defensa real, no solo para el type-checker.
    if locked_user.totp_secret_encrypted is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Configura 2FA antes de activar premium",
        )

    check_totp_lockout(locked_user)

    if not verify_password(payload.password, locked_user.hashed_password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Contraseña incorrecta"
        )

    try:
        secret = decrypt_totp_secret(locked_user.totp_secret_encrypted)
    except TOTPDecryptionError as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Error interno al verificar 2FA. Contacta con soporte.",
        ) from exc

    if not verify_totp_code(secret, payload.totp_code):
        register_totp_failure(locked_user, db)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Código TOTP incorrecto"
        )

    register_totp_success(locked_user, db)
    locked_user.is_premium = True
    db.commit()
    db.refresh(locked_user)
    return locked_user


@router.post("/me/premium/deactivate", response_model=UserRead)
def deactivate_premium(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> User:
    current_user.is_premium = False
    db.commit()
    db.refresh(current_user)
    return current_user


@router.get("/me/recommendations", response_model=list[SongRead])
def get_my_recommendations(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[Song]:
    """Lee las recomendaciones YA precalculadas por Celery Beat (ver
    app/worker.py) - toda la agregación pesada ocurre en la tarea periódica,
    no aquí (ese es justo el punto de "recomendaciones precalculadas": este
    endpoint es un GET barato, no un cálculo en vivo).

    Sin recomendaciones todavía (usuario nuevo, o Beat aún no corrió su
    primer ciclo) es un estado NORMAL y esperado, no una caída de servicio -
    a diferencia de la búsqueda en Meilisearch (Fase 10), que responde 503
    si el motor no está disponible: aquí se devuelve 200 con lista vacía."""
    song_ids = get_recommendations(redis_client, current_user.id)
    if not song_ids:
        return []

    songs = db.scalars(select(Song).where(Song.id.in_(song_ids))).all()
    songs_by_id = {song.id: song for song in songs}
    # Preserva el orden de Redis (el ranking), no el orden natural de la
    # consulta SQL - un id colgante (canción borrada en el futuro) se omite
    # en vez de romper la respuesta.
    return [songs_by_id[song_id] for song_id in song_ids if song_id in songs_by_id]
