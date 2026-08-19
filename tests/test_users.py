from fastapi.testclient import TestClient


def _register_and_login(
    client: TestClient, email: str = "premium@example.com", password: str = "supersecret"
):
    client.post("/auth/register", json={"email": email, "password": password})
    login_response = client.post("/auth/login", data={"username": email, "password": password})
    return login_response.json()["access_token"]


def test_update_premium_status_requires_auth(client: TestClient):
    response = client.patch("/users/me/premium", json={"is_premium": True})
    assert response.status_code == 401


def test_update_premium_status_activates_and_deactivates(client: TestClient):
    access_token = _register_and_login(client)
    headers = {"Authorization": f"Bearer {access_token}"}

    activate_response = client.patch(
        "/users/me/premium", json={"is_premium": True}, headers=headers
    )
    assert activate_response.status_code == 200
    assert activate_response.json()["is_premium"] is True

    me_response = client.get("/auth/me", headers=headers)
    assert me_response.json()["is_premium"] is True

    deactivate_response = client.patch(
        "/users/me/premium", json={"is_premium": False}, headers=headers
    )
    assert deactivate_response.status_code == 200
    assert deactivate_response.json()["is_premium"] is False

    me_response_after = client.get("/auth/me", headers=headers)
    assert me_response_after.json()["is_premium"] is False


def test_update_premium_status_rejects_invalid_token(client: TestClient):
    response = client.patch(
        "/users/me/premium",
        json={"is_premium": True},
        headers={"Authorization": "Bearer not-a-real-token"},
    )
    assert response.status_code == 401


def test_update_premium_status_rejects_missing_field(client: TestClient):
    access_token = _register_and_login(client, email="missingfield@example.com")
    headers = {"Authorization": f"Bearer {access_token}"}

    response = client.patch("/users/me/premium", json={}, headers=headers)
    assert response.status_code == 422


def test_update_premium_status_rejects_non_coercible_type(client: TestClient):
    access_token = _register_and_login(client, email="badtype@example.com")
    headers = {"Authorization": f"Bearer {access_token}"}

    # Pydantic v2 en modo lax SÍ coacciona "true"/"1"/etc a bool; un string
    # arbitrario no coercible es lo que realmente prueba la validación de tipo.
    response = client.patch(
        "/users/me/premium", json={"is_premium": "not-a-bool"}, headers=headers
    )
    assert response.status_code == 422
