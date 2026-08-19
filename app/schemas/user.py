from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator


class UserCreate(BaseModel):
    email: EmailStr
    # bcrypt solo usa los primeros 72 bytes (ver app/core/security.py); el límite
    # de 128 es solo para rechazar payloads absurdamente largos, no por bcrypt.
    password: str = Field(min_length=8, max_length=128)

    @field_validator("password")
    @classmethod
    def _reject_nul_byte(cls, value: str) -> str:
        if "\x00" in value:
            raise ValueError("La contraseña no puede contener el carácter NUL")
        return value


class UserRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    email: EmailStr
    is_active: bool
    created_at: datetime


class Token(BaseModel):
    access_token: str
    token_type: str = "bearer"


class TokenPayload(BaseModel):
    sub: str
    type: str
    iat: datetime
    exp: datetime
