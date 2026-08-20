import logging

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

from app.core.config import settings

logger = logging.getLogger(__name__)

# Timeouts cortos y explícitos: sin esto, boto3 usa sus defaults (varias
# decenas de segundos) y un MinIO inalcanzable deja el arranque de la app
# colgado indefinidamente en ensure_bucket_exists() (verificado empíricamente
# durante la implementación de Fase 8) en vez de fallar rápido.
#
# region_name explícito (Fase 9): antes se dejaba a la resolución implícita de
# boto3 (funcionaba, pero dependía del entorno - variables AWS_* ausentes
# podrían dar NoRegionError en otro contexto). Fijarlo hace el comportamiento
# determinista sin depender del entorno. Nota (verificado empíricamente): las
# URLs que genera boto3.generate_presigned_url contra este endpoint_url
# custom resultan ser SigV2 (query-auth clásico), no SigV4 - boto3 no fuerza
# SigV4 salvo que se configure explícitamente Config(signature_version="s3v4"),
# cosa que no se hace aquí. region_name sigue siendo inofensivo aunque SigV2
# no lo use. MinIO ignora la región salvo que se configure MINIO_REGION.
_config = Config(
    connect_timeout=5,
    read_timeout=10,
    retries={"max_attempts": 1},
    region_name="us-east-1",
)

_client = boto3.client(
    "s3",
    endpoint_url=settings.s3_endpoint_url,
    aws_access_key_id=settings.s3_access_key,
    aws_secret_access_key=settings.s3_secret_key,
    config=_config,
)

# Cliente SEPARADO solo para firmar URLs (Fase 9), con un endpoint distinto
# (s3_public_endpoint_url) - el que debe poder resolver el NAVEGADOR del
# cliente, no necesariamente el mismo que usa este proceso servidor-a-servidor
# para upload_file/download_file/etc. (dentro de docker-compose.yml, este
# proceso habla con MinIO por el hostname interno "minio:9000", pero un
# navegador no puede resolver eso). "Público" se refiere a ALCANZABILIDAD DE
# RED (qué host ve el navegador), NO a hacer el bucket públicamente
# accesible - el bucket sigue privado, toda request sigue exigiendo la firma
# embebida en la URL (SigV2 en la práctica, ver generate_presigned_url abajo).
# generate_presigned_url no hace ninguna llamada de red (es una operación
# criptográfica local), así que no hay riesgo de bloqueo/timeout aquí pese a
# mantener el mismo _config con timeouts.
_public_client = boto3.client(
    "s3",
    endpoint_url=settings.s3_public_endpoint_url,
    aws_access_key_id=settings.s3_access_key,
    aws_secret_access_key=settings.s3_secret_key,
    config=_config,
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


def generate_presigned_url(key: str, expires_in: int) -> str:
    """URL firmada (SigV2 en la práctica, boto3 no fuerza SigV4 para un
    endpoint custom sin configurarlo explícitamente - verificado empíricamente,
    ver docs/architecture.md) contra s3_public_endpoint_url (no
    s3_endpoint_url) - ver comentario de _public_client arriba. Usada por el
    endpoint de streaming (Fase 9) para que el cliente (ej. un <audio src>)
    pueda pedir los bytes directo a MinIO, con soporte nativo de Range
    Requests, sin pasar por ningún header de autorización especial."""
    url: str = _public_client.generate_presigned_url(
        "get_object",
        Params={"Bucket": settings.s3_bucket_name, "Key": key},
        ExpiresIn=expires_in,
    )
    return url


def delete_object(key: str) -> None:
    """Best-effort: usada solo para limpieza en caso de fallo a mitad del
    pipeline de subida, nunca para un endpoint DELETE (fuera de alcance de
    esta fase). Un fallo al borrar se loguea pero no se propaga - no debe
    bloquear el flujo principal que la llama."""
    try:
        _client.delete_object(Bucket=settings.s3_bucket_name, Key=key)
    except ClientError:
        logger.exception("No se pudo borrar el objeto %s de MinIO (best-effort)", key)
