from datetime import datetime

from pydantic import BaseModel, Field


class PlaybackStateUpdate(BaseModel):
    song_id: int
    position_seconds: float = Field(ge=0)
    is_playing: bool
    device_id: str = Field(min_length=1, max_length=100)


class PlaybackState(BaseModel):
    song_id: int
    position_seconds: float
    is_playing: bool
    device_id: str
    # Sellado por el servidor al recibir el PUT, nunca por el cliente - mismo
    # principio de no confiar en relojes de cliente ya aplicado a created_at
    # de Song / played_at de SongPlay.
    updated_at: datetime
