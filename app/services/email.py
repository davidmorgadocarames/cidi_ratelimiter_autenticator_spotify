import logging
import smtplib
from email.message import EmailMessage

from app.core.config import settings

logger = logging.getLogger(__name__)

# Timeout corto y explícito, no opcional: sin él, un Mailhog inalcanzable por un
# fallo de red silencioso (no "connection refused", que falla rápido) puede
# colgar el hilo del request indefinidamente - mismo motivo que ya justifica
# los timeouts explícitos de boto3/Meilisearch (Fases 8/10).
_SMTP_TIMEOUT_SECONDS = 5


def send_verification_email(to_email: str, token: str) -> bool:
    """Best-effort, nunca propaga - mismo patrón que search.index_song (Fase
    10): el registro no debe fallar porque Mailhog esté caído. Devuelve bool
    para que quien la llama decida qué comunicar internamente (logging).

    Fallo de envío completamente silencioso hacia el cliente HTTP - riesgo
    aceptado y documentado (ver docs/architecture.md), mismo patrón ya
    aceptado para search.index_song (una subida de canción con Meilisearch
    caído también responde 201 sin avisar que no quedó indexada)."""
    link = f"{settings.app_public_url}/auth/verify-email?token={token}"

    message = EmailMessage()
    message["Subject"] = "Verifica tu email - CIDI Spotify Clone"
    message["From"] = settings.smtp_from_email
    message["To"] = to_email
    message.set_content(
        "Gracias por registrarte. Verifica tu email visitando este enlace "
        f"(caduca en {settings.email_verification_token_expire_hours}h):\n\n{link}"
    )

    try:
        with smtplib.SMTP(
            settings.smtp_host, settings.smtp_port, timeout=_SMTP_TIMEOUT_SECONDS
        ) as smtp:
            smtp.send_message(message)
    except (OSError, smtplib.SMTPException):
        # OSError cubre fallos de conexión/timeout (ConnectionRefusedError,
        # TimeoutError); smtplib.SMTPException cubre errores del propio
        # protocolo SMTP (ej. destinatario rechazado) una vez conectado.
        logger.exception("No se pudo enviar el email de verificación a %s", to_email)
        return False

    return True
