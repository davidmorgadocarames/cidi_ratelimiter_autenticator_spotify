from datetime import datetime

from sqlalchemy import TIMESTAMP, Boolean, ForeignKey, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.session import Base


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(
        String(255), unique=True, index=True, nullable=False
    )
    hashed_password: Mapped[str] = mapped_column(String(255), nullable=False)
    is_active: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default="true", nullable=False
    )
    is_premium: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="false", nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(), nullable=False
    )

    # default=False (Python) pero server_default="true" (DB) - deliberadamente
    # distintos. El default de Python es lo que aplica a cada User(...) nuevo
    # creado por register() (SQLAlchemy incluye el valor calculado en el INSERT,
    # el server_default no llega a intervenir) - los registros NUEVOS nacen sin
    # verificar. El server_default="true" es la política de backfill que aplica
    # la propia migración a las filas YA EXISTENTES - sin esto, aplicar la
    # migración dejaría esas cuentas bloqueadas de login de la noche a la
    # mañana por una verificación que nunca se les pidió hacer.
    email_verified: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="true", nullable=False
    )
    email_verification_token_hash: Mapped[str | None] = mapped_column(
        String(64), unique=True, index=True, nullable=True
    )
    email_verification_token_expires_at: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )

    # --- 2FA (TOTP) ---
    # "Activado" (totp_enabled, ver abajo) exige AMBAS no-nulas: secreto guardado
    # y confirmado con un código válido. Mientras solo hay secreto pero no
    # confirmación, el 2FA está "pendiente" (setup a medias).
    totp_secret_encrypted: Mapped[str | None] = mapped_column(
        String(255), nullable=True
    )
    totp_confirmed_at: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )
    # Lockout ad-hoc de intentos de código TOTP (setup/verify y activación de
    # premium comparten este contador: ambos son la misma prueba de posesión
    # del dispositivo). Ver app/api/totp.py.
    totp_failed_attempts: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0", nullable=False
    )
    totp_locked_until: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )

    @property
    def totp_enabled(self) -> bool:
        return (
            self.totp_secret_encrypted is not None
            and self.totp_confirmed_at is not None
        )


class RefreshToken(Base):
    __tablename__ = "refresh_tokens"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # Se mantiene igual a través de todas las rotaciones de una misma sesión de login;
    # permite revocar toda la cadena si se detecta reuso de un token ya rotado.
    family_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    token_hash: Mapped[str] = mapped_column(
        String(64), unique=True, index=True, nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False
    )
    revoked_at: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )
    replaced_by_id: Mapped[int | None] = mapped_column(
        ForeignKey("refresh_tokens.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(), nullable=False
    )
