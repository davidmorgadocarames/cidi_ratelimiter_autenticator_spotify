import uuid
from datetime import datetime, timedelta, timezone

import jwt
from fastapi import APIRouter, Cookie, Depends, HTTPException, Response, status
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.security import (
    create_access_token,
    decode_access_token,
    generate_refresh_token,
    hash_password,
    hash_refresh_token,
    verify_password,
)
from app.db.session import get_db
from app.models.user import RefreshToken, User
from app.schemas.user import Token, TokenPayload, UserCreate, UserRead

# Hash bcrypt "dummy" contra el que se compara cuando el usuario no existe, para que
# /auth/login tarde lo mismo con email inexistente que con password incorrecta y no
# se pueda enumerar qué emails están registrados por diferencia de tiempo de respuesta.
_DUMMY_PASSWORD_HASH = hash_password(str(uuid.uuid4()))

REFRESH_COOKIE_NAME = "refresh_token"

router = APIRouter(prefix="/auth", tags=["auth"])
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/login")


def _set_refresh_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        key=REFRESH_COOKIE_NAME,
        value=token,
        httponly=True,
        secure=settings.cookie_secure,
        samesite="lax",
        max_age=settings.refresh_token_expire_days * 24 * 3600,
        path="/auth",
    )


def _create_refresh_token_record(
    db: Session, user_id: int, family_id: str
) -> tuple[RefreshToken, str]:
    raw_token = generate_refresh_token()
    expires_at = datetime.now(timezone.utc) + timedelta(
        days=settings.refresh_token_expire_days
    )
    record = RefreshToken(
        user_id=user_id,
        family_id=family_id,
        token_hash=hash_refresh_token(raw_token),
        expires_at=expires_at,
    )
    db.add(record)
    db.flush()
    return record, raw_token


def get_current_user(
    token: str = Depends(oauth2_scheme), db: Session = Depends(get_db)
) -> User:
    credentials_error = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="No se pudo validar el token",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        raw_payload = decode_access_token(token)
        token_payload = TokenPayload.model_validate(raw_payload)
        user_id = int(token_payload.sub)
    except (jwt.PyJWTError, ValidationError, ValueError) as exc:
        raise credentials_error from exc

    if token_payload.type != "access":
        raise credentials_error

    user = db.get(User, user_id)
    if user is None or not user.is_active:
        raise credentials_error
    return user


@router.post("/register", response_model=UserRead, status_code=status.HTTP_201_CREATED)
def register(payload: UserCreate, db: Session = Depends(get_db)) -> User:
    # Chequeo rápido para el caso común; no basta por sí solo bajo concurrencia
    # (dos requests con el mismo email pueden pasar ambos este SELECT), de ahí el
    # try/except IntegrityError de abajo, que es la guarda real contra la carrera.
    existing = db.scalar(select(User).where(User.email == payload.email))
    if existing is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="Email ya registrado"
        )

    user = User(email=payload.email, hashed_password=hash_password(payload.password))
    db.add(user)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="Email ya registrado"
        ) from None
    db.refresh(user)
    return user


@router.post("/login", response_model=Token)
def login(
    response: Response,
    form_data: OAuth2PasswordRequestForm = Depends(),
    db: Session = Depends(get_db),
) -> Token:
    user = db.scalar(select(User).where(User.email == form_data.username))
    if user is None:
        # Se ejecuta un hash bcrypt igualmente (contra un hash "dummy") para que la
        # respuesta tarde lo mismo que con un email existente + password incorrecta,
        # y así no se pueda enumerar emails registrados por diferencia de tiempo.
        verify_password(form_data.password, _DUMMY_PASSWORD_HASH)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Credenciales incorrectas",
            headers={"WWW-Authenticate": "Bearer"},
        )
    if not verify_password(form_data.password, user.hashed_password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Credenciales incorrectas",
            headers={"WWW-Authenticate": "Bearer"},
        )
    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Usuario inactivo"
        )

    family_id = str(uuid.uuid4())
    _, raw_refresh_token = _create_refresh_token_record(db, user.id, family_id)
    db.commit()

    _set_refresh_cookie(response, raw_refresh_token)
    access_token = create_access_token(subject=str(user.id))
    return Token(access_token=access_token)


@router.post("/refresh", response_model=Token)
def refresh(
    response: Response,
    refresh_token: str | None = Cookie(default=None, alias=REFRESH_COOKIE_NAME),
    db: Session = Depends(get_db),
) -> Token:
    if refresh_token is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Falta el refresh token"
        )

    token_hash = hash_refresh_token(refresh_token)
    # with_for_update(): si dos requests llegan casi a la vez con el mismo token
    # (doble pestaña, reintento de red), la segunda se bloquea hasta que la primera
    # haga commit y ve el registro ya revocado -> entra por la rama de "reuso
    # detectado" de abajo. Sin este lock, ambas leerían revoked_at IS NULL y
    # emitirían dos refresh tokens activos para la misma family_id.
    record = db.scalar(
        select(RefreshToken)
        .where(RefreshToken.token_hash == token_hash)
        .with_for_update()
    )

    if record is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Refresh token inválido"
        )

    if record.revoked_at is not None:
        # Reuso de un token ya rotado: se asume robo y se revoca toda la cadena de
        # sesión.
        now = datetime.now(timezone.utc)
        db.query(RefreshToken).filter(
            RefreshToken.family_id == record.family_id,
            RefreshToken.revoked_at.is_(None),
        ).update({"revoked_at": now})
        db.commit()
        response.delete_cookie(REFRESH_COOKIE_NAME, path="/auth")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Refresh token inválido"
        )

    if record.expires_at < datetime.now(timezone.utc):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Refresh token expirado"
        )

    new_record, raw_refresh_token = _create_refresh_token_record(
        db, record.user_id, record.family_id
    )
    record.revoked_at = datetime.now(timezone.utc)
    record.replaced_by_id = new_record.id
    db.commit()

    _set_refresh_cookie(response, raw_refresh_token)
    access_token = create_access_token(subject=str(record.user_id))
    return Token(access_token=access_token)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout(
    response: Response,
    refresh_token: str | None = Cookie(default=None, alias=REFRESH_COOKIE_NAME),
    db: Session = Depends(get_db),
) -> None:
    if refresh_token is not None:
        token_hash = hash_refresh_token(refresh_token)
        record = db.scalar(
            select(RefreshToken).where(RefreshToken.token_hash == token_hash)
        )
        if record is not None and record.revoked_at is None:
            record.revoked_at = datetime.now(timezone.utc)
            db.commit()
    response.delete_cookie(REFRESH_COOKIE_NAME, path="/auth")


@router.get("/me", response_model=UserRead)
def read_current_user(current_user: User = Depends(get_current_user)) -> User:
    return current_user
