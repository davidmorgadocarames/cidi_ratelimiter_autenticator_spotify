from datetime import datetime

from sqlalchemy import TIMESTAMP, Float, ForeignKey, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.session import Base


class Song(Base):
    __tablename__ = "songs"

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    artist: Mapped[str] = mapped_column(String(255), nullable=False)
    uploaded_by_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # "pending" reservado para cuando la subida pase a ser async - la Fase 11
    # introdujo Celery pero acotado a recomendaciones (decisión explícita del
    # usuario, ver docs/architecture.md), la subida sigue síncrona dentro del
    # propio request, así que una fila nueva nace directamente en "processing"
    # (nunca se observa un "pending" real todavía, sigue pendiente de una
    # fase futura).
    status: Mapped[str] = mapped_column(
        String(20), default="processing", server_default="processing", nullable=False
    )
    # Key fija (original/{id}/source, sin extensión) - nunca derivada del nombre
    # de archivo del cliente, ver app/services/storage.py.
    original_object_key: Mapped[str] = mapped_column(String(512), nullable=False)
    transcoded_object_key: Mapped[str | None] = mapped_column(
        String(512), nullable=True
    )
    duration_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)
    # Declarado por el cliente en el multipart - solo informativo, nunca usado
    # para decidir si el archivo es audio de verdad (eso lo hace ffprobe).
    content_type: Mapped[str] = mapped_column(String(100), nullable=False)
    file_size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    # Mensaje genérico y seguro (nunca stderr crudo de ffmpeg) - el catálogo es
    # público, así que cualquier usuario autenticado puede ver este campo.
    error_message: Mapped[str | None] = mapped_column(String(500), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(), nullable=False
    )
