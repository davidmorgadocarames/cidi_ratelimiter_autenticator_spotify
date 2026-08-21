from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.api.auth import get_current_user
from app.core.redis_client import redis_client
from app.db.session import get_db
from app.models.song import Song
from app.models.user import User
from app.schemas.playback import PlaybackState, PlaybackStateUpdate
from app.services.playback import get_playback_state, set_playback_state

# Mismo prefijo "/users" que app/api/users.py (URL final /users/me/playback),
# archivo separado por dominio de recurso propio - mismo criterio ya usado
# para auth.py/totp.py/songs.py.
router = APIRouter(prefix="/users", tags=["playback"])


@router.put("/me/playback", response_model=PlaybackState)
def update_playback_state(
    payload: PlaybackStateUpdate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> PlaybackState:
    song = db.get(Song, payload.song_id)
    if song is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Canción no encontrada"
        )
    if song.status != "ready":
        # Whitelist ("!= ready"), no blacklist - mismo motivo ya documentado
        # en get_song_stream_url (Fase 9).
        detail = (
            "Todavía se está procesando"
            if song.status == "processing"
            else "El procesamiento de esta canción falló"
        )
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=detail)

    state = PlaybackState(
        song_id=payload.song_id,
        position_seconds=payload.position_seconds,
        is_playing=payload.is_playing,
        device_id=payload.device_id,
        # Sellado por el SERVIDOR al recibir la request, nunca por el cliente.
        updated_at=datetime.now(timezone.utc),
    )
    # model_dump(mode="json") vuelca updated_at como string ISO 8601, no un
    # objeto datetime - json.dumps (dentro de set_playback_state) no sabe
    # serializar datetime directamente.
    set_playback_state(redis_client, current_user.id, state.model_dump(mode="json"))
    return state


@router.get("/me/playback", response_model=PlaybackState)
def get_current_playback_state(
    current_user: User = Depends(get_current_user),
) -> PlaybackState:
    """404 si no hay estado, a diferencia de GET /users/me/recommendations
    (Fase 11) o GET /songs/popular (Fase 12), que devuelven 200 + lista vacía:
    aquí el recurso es un objeto singular, no una lista - "nunca se reportó
    ningún estado" no tiene una representación natural como objeto vacío, así
    que se modela como recurso inexistente (semántica REST estándar)."""
    state = get_playback_state(redis_client, current_user.id)
    if state is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No hay ningún estado de reproducción registrado",
        )
    return PlaybackState(**state)
