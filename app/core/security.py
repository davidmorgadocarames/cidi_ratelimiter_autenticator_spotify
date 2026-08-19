import hashlib
import secrets
from datetime import datetime, timedelta, timezone

import bcrypt
import jwt
import pyotp
from cryptography.fernet import Fernet, InvalidToken

from app.core.config import settings

# bcrypt solo procesa los primeros 72 bytes de la contraseña; se trunca de forma
# explícita para evitar que la librería falle con contraseñas más largas.
_BCRYPT_MAX_BYTES = 72


def hash_password(password: str) -> str:
    truncated = password.encode("utf-8")[:_BCRYPT_MAX_BYTES]
    return bcrypt.hashpw(truncated, bcrypt.gensalt()).decode("utf-8")


def verify_password(password: str, hashed_password: str) -> bool:
    truncated = password.encode("utf-8")[:_BCRYPT_MAX_BYTES]
    return bcrypt.checkpw(truncated, hashed_password.encode("utf-8"))


def create_access_token(subject: str) -> str:
    now = datetime.now(timezone.utc)
    payload = {
        "sub": subject,
        "type": "access",
        "iat": now,
        "exp": now + timedelta(minutes=settings.access_token_expire_minutes),
    }
    return jwt.encode(payload, settings.jwt_secret_key, algorithm=settings.jwt_algorithm)


def decode_access_token(token: str) -> dict:
    return jwt.decode(token, settings.jwt_secret_key, algorithms=[settings.jwt_algorithm])


def generate_refresh_token() -> str:
    return secrets.token_urlsafe(32)


def hash_refresh_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class TOTPDecryptionError(Exception):
    """El secreto TOTP no se pudo descifrar (TOTP_ENCRYPTION_KEY rotada o dato corrupto)."""


def generate_totp_secret() -> str:
    return pyotp.random_base32()


def _totp_fernet() -> Fernet:
    return Fernet(settings.totp_encryption_key.encode("utf-8"))


def encrypt_totp_secret(secret: str) -> str:
    return _totp_fernet().encrypt(secret.encode("utf-8")).decode("utf-8")


def decrypt_totp_secret(encrypted_secret: str) -> str:
    try:
        return _totp_fernet().decrypt(encrypted_secret.encode("utf-8")).decode("utf-8")
    except (InvalidToken, ValueError) as exc:
        # InvalidToken: clave rotada o payload cifrado pero corrupto/manipulado.
        # ValueError (ej. binascii.Error): ni siquiera es base64 urlsafe válido -
        # Fernet.decrypt lo intenta decodificar antes de validar el token.
        raise TOTPDecryptionError(
            "No se pudo descifrar el secreto TOTP (clave rotada o dato corrupto)."
        ) from exc


def verify_totp_code(secret: str, code: str, valid_window: int = 1) -> bool:
    # valid_window=1 => tolera hasta 1 paso de 30s antes/después (±30s) por
    # desfase de reloj entre servidor y dispositivo. pyotp compara con
    # hmac.compare_digest internamente (resistente a timing attacks).
    return pyotp.TOTP(secret).verify(code, valid_window=valid_window)
