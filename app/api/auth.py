import uuid
from datetime import datetime, timedelta, timezone

import jwt
from fastapi import (
    APIRouter,
    BackgroundTasks,
    Cookie,
    Depends,
    HTTPException,
    Response,
    status,
)
from fastapi.responses import HTMLResponse
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.security import (
    create_access_token,
    decode_access_token,
    generate_email_verification_token,
    generate_refresh_token,
    hash_email_verification_token,
    hash_password,
    hash_refresh_token,
    verify_password,
)
from app.db.session import SessionLocal, get_db
from app.models.user import RefreshToken, User
from app.schemas.user import (
    ResendVerificationRequest,
    Token,
    TokenPayload,
    UserCreate,
    UserRead,
)
from app.services.email import send_verification_email

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


def _set_new_verification_token(user: User) -> str:
    """Genera un token nuevo y lo guarda (hasheado) en el usuario, invalidando
    cualquier token de verificación anterior - no persiste por sí sola, quien
    llama debe hacer commit."""
    raw_token = generate_email_verification_token()
    user.email_verification_token_hash = hash_email_verification_token(raw_token)
    user.email_verification_token_expires_at = datetime.now(timezone.utc) + timedelta(
        hours=settings.email_verification_token_expire_hours
    )
    return raw_token


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
    raw_token = _set_new_verification_token(user)
    db.add(user)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="Email ya registrado"
        ) from None
    db.refresh(user)

    # Síncrono a propósito (no BackgroundTasks): register() es un único código
    # de negocio sin ramas que distinguir por timing - a diferencia de
    # resend-verification (más abajo), aquí no hay nada que un atacante pueda
    # enumerar comparando latencias.
    send_verification_email(user.email, raw_token)

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
    if not user.email_verified:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Verifica tu email antes de iniciar sesión",
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


_VERIFY_SUCCESS_HTML = """<!doctype html>
<html lang="es"><head><meta charset="utf-8"><title>Email verificado</title></head>
<body><p>Tu email ha sido verificado. Ya puedes <a href="/">iniciar sesión</a>.</p>
</body></html>"""

_VERIFY_ERROR_HTML = """<!doctype html>
<html lang="es"><head><meta charset="utf-8"><title>Enlace inválido</title></head>
<body><p>Este enlace de verificación no es válido o ha caducado - o puede que tu email
ya esté verificado (algunos gestores de correo/antivirus abren los enlaces de forma
automática por seguridad, antes de que llegues a hacer clic tú mismo). Prueba a
<a href="/">iniciar sesión</a> directamente; si no funciona, pide un enlace nuevo desde
ahí.</p></body></html>"""


@router.get("/verify-email", response_class=HTMLResponse)
def verify_email(token: str, db: Session = Depends(get_db)) -> HTMLResponse:
    token_hash = hash_email_verification_token(token)
    user = db.scalar(
        select(User).where(User.email_verification_token_hash == token_hash)
    )
    if user is None or (
        user.email_verification_token_expires_at is not None
        and user.email_verification_token_expires_at < datetime.now(timezone.utc)
    ):
        return HTMLResponse(content=_VERIFY_ERROR_HTML, status_code=400)

    user.email_verified = True
    user.email_verification_token_hash = None
    user.email_verification_token_expires_at = None
    db.commit()

    return HTMLResponse(content=_VERIFY_SUCCESS_HTML)


def _process_resend_verification(email: str) -> None:
    """Corre en BackgroundTasks, después de que la respuesta HTTP ya se envió
    al cliente - TODO el trabajo con efecto (lookup, generar token, commit,
    envío SMTP) vive aquí, no en el handler síncrono de abajo. Hallazgo real
    de la revisión de seguridad post-implementación: con el lookup + commit
    en el handler síncrono, solo la rama "cuenta existe y no verificada"
    hacía un db.commit() antes de devolver la respuesta - un commit síncrono
    a Postgres es medible por timing, así que esa rama sí se podía distinguir
    de las otras dos (cuenta inexistente / ya verificada) pese a que el
    CUERPO de la respuesta ya era idéntico en las tres. Con el lookup también
    dentro de la tarea en background, el handler no toca la DB en absoluto -
    las tres ramas son ahora indistinguibles por timing, no solo por cuerpo.
    Abre su propia sesión (SessionLocal, no Depends(get_db)): la sesión de la
    request ya se cerró para cuando esta tarea corre."""
    db = SessionLocal()
    try:
        user = db.scalar(select(User).where(User.email == email))
        if user is not None and not user.email_verified:
            raw_token = _set_new_verification_token(user)
            db.commit()
            send_verification_email(user.email, raw_token)
    finally:
        db.close()


@router.post("/resend-verification", status_code=status.HTTP_200_OK)
def resend_verification(
    payload: ResendVerificationRequest,
    background_tasks: BackgroundTasks,
) -> dict[str, str]:
    # Respuesta genérica SIEMPRE la misma, exista o no la cuenta, esté o no ya
    # verificada (mismo principio anti-enumeración que login() con el hash
    # "dummy"). El handler no toca la DB en absoluto (ver
    # _process_resend_verification) - agenda el trabajo real incondicional y
    # devuelve de inmediato, así las tres ramas son indistinguibles también
    # por timing, no solo por cuerpo.
    background_tasks.add_task(_process_resend_verification, payload.email)
    return {
        "detail": "Si la cuenta existe y no está verificada, se ha enviado un email."
    }
