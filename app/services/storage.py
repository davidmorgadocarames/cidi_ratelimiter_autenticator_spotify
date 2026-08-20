import logging

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

from app.core.config import settings

logger = logging.getLogger(__name__)

# Timeouts cortos y explícitos: sin esto, boto3 usa sus defaults (varias
# decenas de segundos) y un MinIO inalcanzable deja el arranque de la app
# colgado indefinidamente en ensure_bucket_exists() (verificado empíricamente
# durante la implementación de esta fase) en vez de fallar rápido.
_client = boto3.client(
    "s3",
    endpoint_url=settings.s3_endpoint_url,
    aws_access_key_id=settings.s3_access_key,
    aws_secret_access_key=settings.s3_secret_key,
    config=Config(connect_timeout=5, read_timeout=10, retries={"max_attempts": 1}),
)


def ensure_bucket_exists() -> None:
    """Se llama UNA vez al arrancar la app (evento startup de FastAPI) o al
    principio de la sesión de tests - nunca perezosamente por request, para
    evitar el TOCTOU de dos primeras-subidas concurrentes llamando ambas a
    create_bucket (MinIO devolvería BucketAlreadyOwnedByYou al perdedor)."""
    try:
        _client.head_bucket(Bucket=settings.s3_bucket_name)
    except ClientError:
        _client.create_bucket(Bucket=settings.s3_bucket_name)


def upload_file(key: str, local_path: str, content_type: str) -> None:
    _client.upload_file(
        local_path,
        settings.s3_bucket_name,
        key,
        ExtraArgs={"ContentType": content_type},
    )


def download_file(key: str, local_path: str) -> None:
    _client.download_file(settings.s3_bucket_name, key, local_path)


def delete_object(key: str) -> None:
    """Best-effort: usada solo para limpieza en caso de fallo a mitad del
    pipeline de subida, nunca para un endpoint DELETE (fuera de alcance de
    esta fase). Un fallo al borrar se loguea pero no se propaga - no debe
    bloquear el flujo principal que la llama."""
    try:
        _client.delete_object(Bucket=settings.s3_bucket_name, Key=key)
    except ClientError:
        logger.exception("No se pudo borrar el objeto %s de MinIO (best-effort)", key)
