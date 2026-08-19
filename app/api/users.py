from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.api.auth import get_current_user
from app.api.totp import (
    check_totp_lockout,
    lock_user_for_totp_check,
    register_totp_failure,
    register_totp_success,
)
from app.core.security import TOTPDecryptionError, decrypt_totp_secret, verify_password, verify_totp_code
from app.db.session import get_db
from app.models.user import User
from app.schemas.user import PremiumActivateRequest, UserRead

router = APIRouter(prefix="/users", tags=["users"])


@router.post("/me/premium/activate", response_model=UserRead)
def activate_premium(
    payload: PremiumActivateRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> User:
    if not current_user.totp_enabled:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Configura 2FA antes de activar premium",
        )

    locked_user = lock_user_for_totp_check(db, current_user.id)

    check_totp_lockout(locked_user)

    if not verify_password(payload.password, locked_user.hashed_password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Contraseña incorrecta"
        )

    try:
        secret = decrypt_totp_secret(locked_user.totp_secret_encrypted)
    except TOTPDecryptionError as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Error interno al verificar 2FA. Contacta con soporte.",
        ) from exc

    if not verify_totp_code(secret, payload.totp_code):
        register_totp_failure(locked_user, db)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Código TOTP incorrecto"
        )

    register_totp_success(locked_user, db)
    locked_user.is_premium = True
    db.commit()
    db.refresh(locked_user)
    return locked_user


@router.post("/me/premium/deactivate", response_model=UserRead)
def deactivate_premium(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> User:
    current_user.is_premium = False
    db.commit()
    db.refresh(current_user)
    return current_user
