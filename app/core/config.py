from cryptography.fernet import Fernet
from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = (
        "postgresql+psycopg://user:password@localhost:5432/spotify_clone"
    )
    test_database_url: str = (
        "postgresql+psycopg://user:password@localhost:5432/spotify_clone_test"
    )

    jwt_secret_key: str = "change-me"
    jwt_algorithm: str = "HS256"
    access_token_expire_minutes: int = 30
    refresh_token_expire_days: int = 7

    cookie_secure: bool = True

    totp_encryption_key: str = "change-me"

    redis_url: str = "redis://localhost:6379/0"

    s3_endpoint_url: str = "http://localhost:9000"
    s3_access_key: str = "change-me"
    s3_secret_key: str = "change-me"
    s3_bucket_name: str = "songs"
    # Endpoint que debe poder resolver el NAVEGADOR del cliente para consumir
    # una presigned URL (Fase 9) - distinto de s3_endpoint_url, que dentro de
    # docker-compose.yml se sobreescribe al hostname interno "minio:9000"
    # (no resoluble desde fuera del contenedor). No es un secreto, sin
    # validador anti-"change-me".
    s3_public_endpoint_url: str = "http://localhost:9000"

    meilisearch_url: str = "http://localhost:7700"
    meilisearch_api_key: str = "change-me"

    @field_validator("jwt_secret_key")
    @classmethod
    def _reject_placeholder_secret(cls, value: str) -> str:
        if value == "change-me":
            raise ValueError(
                "JWT_SECRET_KEY sigue en el valor placeholder 'change-me'. "
                "Genera uno real (ej. `openssl rand -hex 32`) y ponlo en .env."
            )
        return value

    @field_validator("totp_encryption_key")
    @classmethod
    def _validate_totp_encryption_key(cls, value: str) -> str:
        if value == "change-me":
            raise ValueError(
                "TOTP_ENCRYPTION_KEY sigue en el valor placeholder 'change-me'. "
                "Genera uno real con "
                '`python -c "from cryptography.fernet import Fernet; '
                'print(Fernet.generate_key().decode())"` y ponlo en .env.'
            )
        try:
            Fernet(value.encode("utf-8"))
        except Exception as exc:
            raise ValueError(
                "TOTP_ENCRYPTION_KEY no es una clave Fernet válida "
                "(debe ser base64 urlsafe de 32 bytes)."
            ) from exc
        return value

    @field_validator("s3_access_key", "s3_secret_key")
    @classmethod
    def _reject_placeholder_s3_credentials(cls, value: str) -> str:
        if value == "change-me":
            raise ValueError(
                "S3_ACCESS_KEY/S3_SECRET_KEY siguen en el valor placeholder "
                "'change-me'. Pon las credenciales reales de MinIO en .env "
                "(MINIO_ROOT_USER/MINIO_ROOT_PASSWORD del servicio minio)."
            )
        return value

    @field_validator("meilisearch_api_key")
    @classmethod
    def _reject_placeholder_meilisearch_key(cls, value: str) -> str:
        if value == "change-me":
            raise ValueError(
                "MEILISEARCH_API_KEY sigue en el valor placeholder 'change-me'. "
                "Pon la master key real de Meilisearch en .env "
                "(MEILI_MASTER_KEY del servicio meilisearch)."
            )
        return value


settings = Settings()
