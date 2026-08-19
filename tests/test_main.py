from fastapi.testclient import TestClient


def test_health_is_not_shadowed_by_static_mount(client: TestClient):
    """Regresión: StaticFiles está montado en "/" DESPUÉS de los routers de API en
    app/main.py. Si algún router futuro se registrara por error después del mount,
    sus rutas quedarían tapadas por el catch-all de estáticos (200 HTML en vez del
    JSON esperado) en silencio, sin fallar ruidosamente."""
    response = client.get("/health")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")
    assert response.json() == {"status": "ok"}


def test_auth_routes_are_not_shadowed_by_static_mount(client: TestClient):
    response = client.get("/auth/me")
    assert response.status_code == 401
    assert response.headers["content-type"].startswith("application/json")


def test_static_index_is_served_at_root(client: TestClient):
    response = client.get("/")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
