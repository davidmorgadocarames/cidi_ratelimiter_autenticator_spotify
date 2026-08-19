import base64
import io
from datetime import datetime, timedelta, timezone

import pyotp
import qrcode
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.auth import get_current_user
from app.core.security import (
    TOTPDecryptionError,
    decrypt_totp_secret,
    encrypt_totp_secret,
    generate_totp_secret,
    verify_password,
    verify_totp_code,
)
from app.db.session import get_db
from app.models.user import User
from app.schemas.totp import (
    TOTPSetupRequest,
    TOTPSetupResponse,
    TOTPVerifyRequest,
    TOTPVerifyResponse,
)

router = APIRouter(prefix="/2fa", tags=["2fa"])

# Lockout ad-hoc (sin Redis, eso es Fase 6): un código de 6 dígitos es un
# espacio de búsqueda pequeño y fijo, así que sin ningún límite es fuerza
# bruta explotable de verdad. Compartido con la verificación TOTP de
# /users/me/premium/activate (app/api/users.py) - es la misma prueba de
# posesión del dispositivo.
TOTP_LOCKOUT_MAX_ATTEMPTS = 5
TOTP_LOCKOUT_DURATION = timedelta(minutes=15)


def lock_user_for_totp_check(db: Session, user_id: int) -> User:
    """Recarga al usuario con SELECT ... FOR UPDATE antes de comprobar/actualizar el
    lockout de TOTP. Sin esto, dos requests concurrentes (mismo access token, ej.
    fuerza bruta en paralelo) leerían el mismo totp_failed_attempts antes de que
    ninguna hiciera commit -> "lost update" que neutraliza el lockout por completo
    (mismo patrón de race condition que se corrigió en /auth/refresh en Fase 1).

    populate_existing=True es imprescindible: get_current_user ya cargó este User
    en el identity map de la sesión con un SELECT normal; sin esto, SQLAlchemy
    devolvería el objeto Python cacheado (con el totp_failed_attempts ANTERIOR al
    lock) en vez de refrescarlo con el valor real ya bloqueado en Postgres."""
    return db.execute(
        select(User)
        .where(User.id == user_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).scalar_one()


def check_totp_lockout(user: User) -> None:
    if user.totp_locked_until is not None and user.totp_locked_until > datetime.now(timezone.utc):
        remaining_seconds = (user.totp_locked_until - datetime.now(timezone.utc)).total_seconds()
        remaining_minutes = max(1, int(remaining_seconds // 60) + 1)
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"Demasiados intentos fallidos. Inténtalo de nuevo en {remaining_minutes} min.",
        )


def register_totp_failure(user: User, db: Session) -> None:
    user.totp_failed_attempts += 1
    if user.totp_failed_attempts >= TOTP_LOCKOUT_MAX_ATTEMPTS:
        user.totp_locked_until = datetime.now(timezone.utc) + TOTP_LOCKOUT_DURATION
        user.totp_failed_attempts = 0
    db.commit()


def register_totp_success(user: User, db: Session) -> None:
    user.totp_failed_attempts = 0
    db.commit()


def _build_qr_code_base64(otpauth_uri: str) -> str:
    image = qrcode.make(otpauth_uri)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return base64.b64encode(buffer.getvalue()).decode("utf-8")


@router.post("/setup", response_model=TOTPSetupResponse)
def setup_totp(
    payload: TOTPSetupRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> TOTPSetupResponse:
    if not verify_password(payload.password, current_user.hashed_password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Contraseña incorrecta"
        )

    if current_user.totp_enabled:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="El 2FA ya está activado para esta cuenta",
        )

    secret = generate_totp_secret()
    current_user.totp_secret_encrypted = encrypt_totp_secret(secret)
    current_user.totp_confirmed_at = None
    db.commit()

    otpauth_uri = pyotp.totp.TOTP(secret).provisioning_uri(
        name=current_user.email, issuer_name="CIDI Spotify Clone"
    )
    return TOTPSetupResponse(
        secret=secret,
        otpauth_uri=otpauth_uri,
        qr_code_base64=_build_qr_code_base64(otpauth_uri),
    )


@router.post("/verify", response_model=TOTPVerifyResponse)
def verify_totp(
    payload: TOTPVerifyRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> TOTPVerifyResponse:
    locked_user = lock_user_for_totp_check(db, current_user.id)

    if locked_user.totp_secret_encrypted is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No hay una configuración de 2FA en curso. Llama primero a /2fa/setup.",
        )

    check_totp_lockout(locked_user)

    try:
        secret = decrypt_totp_secret(locked_user.totp_secret_encrypted)
    except TOTPDecryptionError as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Error interno al verificar 2FA. Contacta con soporte.",
        ) from exc

    if not verify_totp_code(secret, payload.code):
        register_totp_failure(locked_user, db)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Código TOTP incorrecto"
        )

    register_totp_success(locked_user, db)

    if locked_user.totp_confirmed_at is None:
        locked_user.totp_confirmed_at = datetime.now(timezone.utc)
        db.commit()
        return TOTPVerifyResponse(enabled=True)

    return TOTPVerifyResponse(valid=True)
