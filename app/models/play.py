from datetime import datetime

from sqlalchemy import TIMESTAMP, ForeignKey, Index, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.session import Base


class SongPlay(Base):
    __tablename__ = "song_plays"
    __table_args__ = (Index("ix_song_plays_user_id_song_id", "user_id", "song_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    song_id: Mapped[int] = mapped_column(
        ForeignKey("songs.id", ondelete="CASCADE"), nullable=False
    )
    # Sin UNIQUE(user_id, song_id) a propósito: cada request de streaming es un
    # evento de reproducción genuino, repetir la misma canción es una señal más
    # fuerte de preferencia, no un duplicado a descartar (Fase 11).
    played_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(), nullable=False
    )
