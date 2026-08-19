from pydantic import BaseModel, Field


class TOTPSetupRequest(BaseModel):
    password: str


class TOTPSetupResponse(BaseModel):
    secret: str
    otpauth_uri: str
    qr_code_base64: str


class TOTPVerifyRequest(BaseModel):
    code: str = Field(pattern=r"^\d{6}$")


class TOTPVerifyResponse(BaseModel):
    enabled: bool = False
    valid: bool = False
