from datetime import datetime

from pydantic import BaseModel, Field, field_validator

from app.schemas.song import SongRead


class PlaylistCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)

    @field_validator("name")
    @classmethod
    def _strip_and_reject_blank(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("El nombre de la playlist no puede estar vacío")
        return stripped


class PlaylistUpdate(PlaylistCreate):
    pass


class PlaylistSongCreate(BaseModel):
    song_id: int


class PlaylistReorderRequest(BaseModel):
    song_ids: list[int]


class PlaylistSongRead(BaseModel):
    song: SongRead
    position: int
    added_at: datetime


class PlaylistRead(BaseModel):
    id: int
    name: str
    created_at: datetime
    song_count: int
    total_duration_seconds: float


class PlaylistDetail(PlaylistRead):
    songs: list[PlaylistSongRead]
