from datetime import datetime

from pydantic import BaseModel, ConfigDict


class SongRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    title: str
    artist: str
    uploaded_by_id: int
    status: str
    duration_seconds: float | None
    content_type: str
    file_size_bytes: int
    error_message: str | None
    created_at: datetime


class SongStreamURL(BaseModel):
    url: str
    expires_in: int
