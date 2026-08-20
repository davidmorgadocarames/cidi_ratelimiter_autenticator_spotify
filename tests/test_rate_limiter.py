import time
from typing import cast

import redis as redis_sync
from fastapi.testclient import TestClient
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route

from app.core.rate_limiter import RateLimitMiddleware

PASSWORD = "supersecret"


def _register_and_login(
    client: TestClient, email: str, password: str = PASSWORD
) -> str:
    client.post("/auth/register", json={"email": email, "password": password})
    login_response = client.post(
        "/auth/login", data={"username": email, "password": password}
    )
    access_token: str = login_response.json()["access_token"]
    return access_token


def _headers(access_token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {access_token}"}


def test_allowed_request_has_rate_limit_headers(client: TestClient) -> None:
    response = client.post(
        "/auth/register", json={"email": "headers@example.com", "password": PASSWORD}
    )
    assert response.status_code == 201
    assert response.headers["X-RateLimit-Limit"] == "5"
    assert response.headers["X-RateLimit-Remaining"] == "4"
    assert "X-RateLimit-Reset" in response.headers


def test_sensitive_tier_blocks_after_capacity_exceeded(client: TestClient) -> None:
    email = "sensitive-limit@example.com"
    # 1er token: registro.
    register_response = client.post(
        "/auth/register", json={"email": email, "password": PASSWORD}
    )
    assert register_response.status_code == 201

    # Tokens 2-5: intentos de login con password incorrecta (mismo bucket "sensitive"
    # por IP - register y login comparten cuota, no son buckets separados).
    for _ in range(4):
        response = client.post(
            "/auth/login", data={"username": email, "password": "wrong-password"}
        )
        assert response.status_code == 401

    # 6ta request "sensitive" desde la misma identidad: bucket agotado.
    blocked_response = client.post(
        "/auth/login", data={"username": email, "password": "wrong-password"}
    )
    assert blocked_response.status_code == 429
    assert blocked_response.json()["detail"] == "Too many requests"
    assert blocked_response.headers["X-RateLimit-Remaining"] == "0"


def test_independent_buckets_per_authenticated_user(client: TestClient) -> None:
    token_a = _register_and_login(client, "bucket-a@example.com")
    token_b = _register_and_login(client, "bucket-b@example.com")

    # Agota el bucket "sensitive" de A vía /2fa/setup con contraseña incorrecta.
    for _ in range(5):
        response = client.post(
            "/2fa/setup", json={"password": "wrong"}, headers=_headers(token_a)
        )
        assert response.status_code == 401

    blocked = client.post(
        "/2fa/setup", json={"password": "wrong"}, headers=_headers(token_a)
    )
    assert blocked.status_code == 429

    # B tiene su propio bucket, sin afectar por lo que le pasó a A.
    response_b = client.post(
        "/2fa/setup", json={"password": "wrong"}, headers=_headers(token_b)
    )
    assert response_b.status_code == 401


def test_authenticated_request_uses_user_id_not_ip(
    client: TestClient, redis_client: redis_sync.Redis
) -> None:
    token = _register_and_login(client, "identity@example.com")
    client.post("/2fa/setup", json={"password": "wrong"}, headers=_headers(token))

    # cast: los stubs de redis-py comparten el tipo de retorno entre el
    # cliente sync y el async (Awaitable[Any] | Any) - en runtime, con el
    # cliente síncrono, esto siempre es la lista real, nunca un awaitable.
    user_keys = cast(list[str], redis_client.keys("ratelimit:sensitive:user:*"))
    assert len(user_keys) == 1


def test_refill_restores_capacity_after_simulated_time_passing(
    client: TestClient, redis_client: redis_sync.Redis
) -> None:
    email = "refill@example.com"
    client.post("/auth/register", json={"email": email, "password": PASSWORD})
    for _ in range(4):
        client.post(
            "/auth/login", data={"username": email, "password": "wrong-password"}
        )

    blocked = client.post(
        "/auth/login", data={"username": email, "password": "wrong-password"}
    )
    assert blocked.status_code == 429

    ip_keys = cast(list[str], redis_client.keys("ratelimit:sensitive:ip:*"))
    assert len(ip_keys) == 1
    # Simula que pasó tiempo de sobra para un refill completo (capacity/refill_rate
    # = 60s en el tier sensitive) sin esperar en tiempo real.
    redis_client.hset(ip_keys[0], "last_refill", str(time.time() - 120))

    recovered = client.post(
        "/auth/login", data={"username": email, "password": "wrong-password"}
    )
    # Ya no 429 (el bucket se rellenó) - vuelve a fallar solo por password incorrecta.
    assert recovered.status_code == 401


def test_login_with_bearer_token_still_rate_limited_by_ip(
    client: TestClient, redis_client: redis_sync.Redis
) -> None:
    """Regresión: adjuntar un Bearer token propio a /auth/login no debe crear
    un bucket "user:*" separado - login es pre-auth y no usa ese header para
    autenticar, así que debe seguir cayendo en el bucket "ip:*" compartido. Si
    no, un atacante podría "comprar" intentos extra registrando cuentas y
    adjuntando sus tokens a intentos de login contra una víctima (bug real
    encontrado en la revisión de seguridad/abogado del diablo de esta fase)."""
    token = _register_and_login(client, "attacker@example.com")  # 2/5 del bucket IP

    for _ in range(3):
        response = client.post(
            "/auth/login",
            data={"username": "victim@example.com", "password": "wrong"},
            headers=_headers(token),
        )
        assert response.status_code == 401

    # register (1) + login (1) del helper + 3 intentos con token adjunto = 5/5.
    blocked = client.post(
        "/auth/login",
        data={"username": "victim@example.com", "password": "wrong"},
        headers=_headers(token),
    )
    assert blocked.status_code == 429

    # Ningún bucket "user:*" se creó desde login/registro pese al Bearer adjunto.
    user_keys = cast(list[str], redis_client.keys("ratelimit:sensitive:user:*"))
    assert user_keys == []


def test_general_tier_blocks_after_capacity_exceeded(client: TestClient) -> None:
    token = _register_and_login(client, "general-limit@example.com")

    for _ in range(60):
        response = client.get("/auth/me", headers=_headers(token))
        assert response.status_code == 200

    blocked = client.get("/auth/me", headers=_headers(token))
    assert blocked.status_code == 429
    assert blocked.json()["detail"] == "Too many requests"
    assert blocked.headers["X-RateLimit-Limit"] == "60"


def test_fail_open_when_redis_unreachable() -> None:
    async def _stub_endpoint(_request: object) -> PlainTextResponse:
        return PlainTextResponse("ok")

    stub_app = Starlette(
        routes=[Route("/auth/login", _stub_endpoint, methods=["POST"])]
    )
    stub_app.add_middleware(RateLimitMiddleware, redis_url="redis://localhost:1/0")

    with TestClient(stub_app) as isolated_client:
        response = isolated_client.post("/auth/login")
        assert response.status_code == 200
        assert response.text == "ok"
