import logging
import time
from dataclasses import dataclass
from typing import Any

import redis
from starlette.concurrency import run_in_threadpool
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp

from app.core.config import settings
from app.core.security import decode_access_token

logger = logging.getLogger(__name__)

# Lee el estado del bucket, calcula el refill según el tiempo transcurrido,
# decrementa y escribe — todo en un único script Lua, que Redis ejecuta de forma
# atómica (serializada). Sin esto, dos requests concurrentes de la misma
# identidad podrían leer el mismo "tokens" antes de que ninguna escriba (el
# mismo tipo de "lost update" que ya se corrigió dos veces en Postgres: refresh
# tokens en Fase 1, lockout de TOTP en Fase 3).
#
# El "return {allowed, tostring(refilled)}" con tostring() explícito no es
# cosmético: Redis trunca a entero los números Lua que un script *devuelve*
# (no los que se pasan como argumento a redis.call), así que sin tostring()
# perderíamos la parte fraccionaria de los tokens restantes.
_TOKEN_BUCKET_SCRIPT = """
local key = KEYS[1]
local capacity = tonumber(ARGV[1])
local refill_rate = tonumber(ARGV[2])
local now = tonumber(ARGV[3])
local requested = tonumber(ARGV[4])

local bucket = redis.call('HMGET', key, 'tokens', 'last_refill')
local tokens = tonumber(bucket[1])
local last_refill = tonumber(bucket[2])

if tokens == nil then
    tokens = capacity
    last_refill = now
end

local elapsed = math.max(0, now - last_refill)
local refilled = math.min(capacity, tokens + elapsed * refill_rate)

local allowed = 0
if refilled >= requested then
    allowed = 1
    refilled = refilled - requested
end

redis.call('HMSET', key, 'tokens', refilled, 'last_refill', now)
local ttl = math.ceil((capacity / refill_rate) * 2)
redis.call('EXPIRE', key, ttl)

return {allowed, tostring(refilled)}
"""


@dataclass(frozen=True)
class Tier:
    name: str
    capacity: int
    refill_rate: float  # tokens por segundo


# Únicos endpoints sin ninguna protección propia contra adivinar credenciales:
# login/registro no tienen lockout, /2fa/setup solo comprueba contraseña.
SENSITIVE = Tier(name="sensitive", capacity=5, refill_rate=1 / 12)
# Resto de la API bajo /auth, /users, /2fa. Incluye /2fa/verify y
# /users/me/premium/activate a propósito (no en SENSITIVE): ya tienen su propio
# lockout de TOTP (5 intentos -> 15 min en Postgres, ver app/api/totp.py), más
# estricto que cualquier tier de este rate limiter — aplicarles además el tier
# sensitive haría que el 429 del middleware (segundos de reset) se disparase
# antes que el 429 del lockout (minutos de reset), con mensajes distintos y
# confusos. Con capacity=60 aquí, el lockout de 5 intentos siempre actúa primero.
GENERAL = Tier(name="general", capacity=60, refill_rate=1.0)

_SENSITIVE_ENDPOINTS = {
    ("POST", "/auth/login"),
    ("POST", "/auth/register"),
    ("POST", "/2fa/setup"),
}

# login/registro son pre-auth: no autentican por el header Authorization (lo
# ignoran por completo), así que un Bearer token adjuntado ahí no demuestra
# nada sobre quién hace la request. Si _resolve_identity lo aceptara igual,
# un atacante podría adjuntar tokens propios (uno por cuenta que registre) a
# sus intentos de login contra una víctima y conseguir un bucket "user:X"
# nuevo por cada token, esquivando por completo el límite por IP que este
# tier existe para imponer (encontrado en la revisión de seguridad/abogado
# del diablo de esta fase). /2fa/setup SÍ exige auth de verdad para llegar al
# handler, así que ahí bucketear por user_id es correcto y deliberado.
_IP_ONLY_ENDPOINTS = {
    ("POST", "/auth/login"),
    ("POST", "/auth/register"),
}

# Prefijos de API cubiertos por el rate limiter. Todo lo demás (estáticos,
# /health) no pasa por Redis en absoluto.
_RATE_LIMITED_PREFIXES = ("/auth/", "/users/", "/2fa/")


def _resolve_tier(method: str, path: str) -> Tier | None:
    if (method, path) in _SENSITIVE_ENDPOINTS:
        return SENSITIVE
    if path.startswith(_RATE_LIMITED_PREFIXES):
        return GENERAL
    return None


def _resolve_identity(request: Request, *, force_ip: bool) -> str:
    """`user:<sub>` si el header Authorization trae un access token válido (no
    toca la DB - solo decodifica la firma, la validación "de verdad" del
    usuario la sigue haciendo get_current_user en cada endpoint); si no,
    `ip:<host>`. `force_ip=True` (login/registro, ver _IP_ONLY_ENDPOINTS)
    ignora cualquier Bearer presente - esos endpoints no lo usan para
    autenticar, así que no es una identidad de fiar ahí. Asunción explícita:
    no hay proxy inverso delante de la API en este despliegue, así que
    request.client.host es la IP real de la conexión TCP, no un header
    (X-Forwarded-For) falsificable — revisar si eso cambia."""
    if not force_ip:
        auth_header = request.headers.get("authorization", "")
        if auth_header.lower().startswith("bearer "):
            token = auth_header[7:]
            try:
                payload = decode_access_token(token)
                sub = payload.get("sub")
                if sub is not None:
                    return f"user:{sub}"
            except Exception:  # noqa: BLE001 - cualquier fallo cae a IP
                pass
    client_host = request.client.host if request.client else "unknown"
    return f"ip:{client_host}"


class RateLimitMiddleware(BaseHTTPMiddleware):
    def __init__(self, app: ASGIApp, redis_url: str | None = None) -> None:
        super().__init__(app)
        # Cliente Redis SÍNCRONO despachado vía threadpool (run_in_threadpool),
        # no redis.asyncio: un cliente async queda ligado al event loop en el que
        # se creó/usó por primera vez, y ese loop no es estable a lo largo de la
        # vida del proceso bajo ciertos escenarios (comprobado con TestClient,
        # que puede usar un loop distinto por instancia; en producción real con
        # un único worker de uvicorn de larga duración no se manifestaría, pero
        # es un riesgo real, no solo un artefacto de tests). El cliente síncrono
        # con threadpool evita por completo la afinidad a un event loop concreto.
        #
        # El ignore de abajo: redis SÍ publica py.typed (a diferencia de qrcode
        # en Fase 4), pero Redis.from_url(cls, url: str, **kwargs) deja **kwargs
        # sin anotar upstream - gap real de tipado, no un módulo faltante.
        self._redis: redis.Redis = redis.from_url(  # type: ignore[no-untyped-call]
            redis_url or settings.redis_url, decode_responses=True
        )
        self._script = self._redis.register_script(_TOKEN_BUCKET_SCRIPT)

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        tier = _resolve_tier(request.method, request.url.path)
        if tier is None:
            return await call_next(request)

        force_ip = (request.method, request.url.path) in _IP_ONLY_ENDPOINTS
        identity = _resolve_identity(request, force_ip=force_ip)
        key = f"ratelimit:{tier.name}:{identity}"
        now = time.time()

        try:
            result: Any = await run_in_threadpool(
                self._script, keys=[key], args=[tier.capacity, tier.refill_rate, now, 1]
            )
        except redis.RedisError:
            # Fail-open: prioriza disponibilidad. Documentado en
            # docs/architecture.md como riesgo aceptado, incluyendo el
            # escenario de ataque (tumbar Redis para eliminar la única
            # protección de fuerza bruta de login/registro). Deliberadamente
            # NO un "except Exception" genérico: eso enmascararía un bug de
            # programación propio (ej. el Lua devolviendo algo inesperado)
            # como si fuera un fallo de Redis, desactivando el rate limiter
            # en silencio en vez de fallar de forma visible (500).
            logger.exception("Rate limiter: fallo hablando con Redis, fail-open")
            return await call_next(request)

        allowed = bool(int(result[0]))
        remaining = float(result[1])

        reset_at = now if remaining >= 1 else now + (1 - remaining) / tier.refill_rate
        headers = {
            "X-RateLimit-Limit": str(tier.capacity),
            "X-RateLimit-Remaining": str(max(0, int(remaining))),
            "X-RateLimit-Reset": str(int(reset_at)),
        }

        if not allowed:
            return JSONResponse(
                status_code=429,
                content={"detail": "Too many requests"},
                headers=headers,
            )

        response = await call_next(request)
        for header_name, header_value in headers.items():
            response.headers[header_name] = header_value
        return response
