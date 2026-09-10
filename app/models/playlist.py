from datetime import datetime

from sqlalchemy import (
    TIMESTAMP,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.session import Base


class Playlist(Base):
    __tablename__ = "playlists"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(), nullable=False
    )


class PlaylistSong(Base):
    __tablename__ = "playlist_songs"
    __table_args__ = (
        UniqueConstraint(
            "playlist_id", "song_id", name="uq_playlist_songs_playlist_id_song_id"
        ),
        # No único a propósito: el renumerado completo de position en cada
        # mutación (add/remove/reorder) pasa transitoriamente por colisiones
        # de position dentro de la misma transacción antes de asentarse -
        # forzar unicidad exigiría DEFERRABLE INITIALLY DEFERRED sin
        # necesidad real, la contigüidad ya está garantizada por construcción
        # (nunca se escribe position de ninguna otra forma).
        Index("ix_playlist_songs_playlist_id_position", "playlist_id", "position"),
        Index("ix_playlist_songs_song_id", "song_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    playlist_id: Mapped[int] = mapped_column(
        ForeignKey("playlists.id", ondelete="CASCADE"), nullable=False
    )
    song_id: Mapped[int] = mapped_column(
        ForeignKey("songs.id", ondelete="CASCADE"), nullable=False
    )
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    added_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(), nullable=False
    )
