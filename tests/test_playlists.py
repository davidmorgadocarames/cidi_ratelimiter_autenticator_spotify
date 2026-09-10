import pyotp
import pytest
from conftest import mark_email_verified
from fastapi.testclient import TestClient
from httpx import Response
from sqlalchemy import text
from sqlalchemy.orm import Session
from test_songs import _create_song_directly
from test_users import _enable_totp

from app.models.user import User

PASSWORD = "supersecret"


def _register_and_login(
    client: TestClient, email: str, password: str = PASSWORD
) -> str:
    client.post("/auth/register", json={"email": email, "password": password})
    mark_email_verified(email)
    login_response = client.post(
        "/auth/login", data={"username": email, "password": password}
    )
    access_token: str = login_response.json()["access_token"]
    return access_token


def _headers(access_token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {access_token}"}


def _get_user_id(db_session: Session, email: str) -> int:
    user = db_session.query(User).filter(User.email == email).one()
    return user.id


def _make_premium_user(client: TestClient, email: str) -> str:
    """Compone _register_and_login + _enable_totp (importado de test_users) +
    activar premium, ya que todo test de playlists necesita pasar el gate."""
    access_token = _register_and_login(client, email)
    secret = _enable_totp(client, access_token)
    code = pyotp.TOTP(secret).now()
    response = client.post(
        "/users/me/premium/activate",
        json={"password": PASSWORD, "totp_code": code},
        headers=_headers(access_token),
    )
    assert response.status_code == 200
    return access_token


def _create_playlist(
    client: TestClient, access_token: str, name: str = "Mi lista"
) -> Response:
    return client.post(
        "/playlists", json={"name": name}, headers=_headers(access_token)
    )


# --- Gate: is_premium && totp_enabled -----------------------------------


def test_gate_blocks_plain_authenticated_user(client: TestClient) -> None:
    access_token = _register_and_login(client, "plain@example.com")
    response = client.get("/playlists", headers=_headers(access_token))
    assert response.status_code == 403


def test_gate_blocks_premium_without_totp(
    client: TestClient, db_session: Session
) -> None:
    email = "premium-no-totp@example.com"
    access_token = _register_and_login(client, email)
    user = db_session.query(User).filter(User.email == email).one()
    user.is_premium = True
    db_session.commit()

    response = client.get("/playlists", headers=_headers(access_token))
    assert response.status_code == 403


def test_gate_allows_premium_and_totp(client: TestClient) -> None:
    access_token = _make_premium_user(client, "gated-ok@example.com")
    response = client.get("/playlists", headers=_headers(access_token))
    assert response.status_code == 200


# --- CRUD ------------------------------------------------------------------


def test_create_list_rename_get_delete_playlist(
    client: TestClient, db_session: Session
) -> None:
    access_token = _make_premium_user(client, "crud@example.com")

    create_response = _create_playlist(client, access_token, "Gym")
    assert create_response.status_code == 201
    body = create_response.json()
    assert body["name"] == "Gym"
    assert body["song_count"] == 0
    assert body["total_duration_seconds"] == 0.0
    playlist_id = body["id"]

    list_response = client.get("/playlists", headers=_headers(access_token))
    assert list_response.status_code == 200
    assert any(p["id"] == playlist_id for p in list_response.json())

    detail_response = client.get(
        f"/playlists/{playlist_id}", headers=_headers(access_token)
    )
    assert detail_response.status_code == 200
    assert detail_response.json()["songs"] == []

    user_id = _get_user_id(db_session, "crud@example.com")
    song_a = _create_song_directly(db_session, user_id, "ready")
    song_b = _create_song_directly(db_session, user_id, "ready")
    for song in (song_a, song_b):
        add_response = client.post(
            f"/playlists/{playlist_id}/songs",
            json={"song_id": song.id},
            headers=_headers(access_token),
        )
        assert add_response.status_code == 201

    detail_with_songs = client.get(
        f"/playlists/{playlist_id}", headers=_headers(access_token)
    ).json()
    assert detail_with_songs["song_count"] == 2
    assert detail_with_songs["total_duration_seconds"] == pytest.approx(
        (song_a.duration_seconds or 0.0) + (song_b.duration_seconds or 0.0)
    )

    rename_response = client.patch(
        f"/playlists/{playlist_id}",
        json={"name": "Gym renombrado"},
        headers=_headers(access_token),
    )
    assert rename_response.status_code == 200
    assert rename_response.json()["name"] == "Gym renombrado"
    assert rename_response.json()["song_count"] == 2

    delete_response = client.delete(
        f"/playlists/{playlist_id}", headers=_headers(access_token)
    )
    assert delete_response.status_code == 204

    list_after_delete = client.get("/playlists", headers=_headers(access_token))
    assert all(p["id"] != playlist_id for p in list_after_delete.json())

    get_after_delete = client.get(
        f"/playlists/{playlist_id}", headers=_headers(access_token)
    )
    assert get_after_delete.status_code == 404

    # Cascada real (ondelete="CASCADE" a nivel de DB, no ORM): sin filas
    # huérfanas en playlist_songs tras borrar la playlist.
    orphan_count = db_session.execute(
        text("SELECT count(*) FROM playlist_songs WHERE playlist_id = :pid"),
        {"pid": playlist_id},
    ).scalar_one()
    assert orphan_count == 0


def test_playlist_name_rejects_blank_after_strip(client: TestClient) -> None:
    access_token = _make_premium_user(client, "blankname@example.com")
    response = _create_playlist(client, access_token, "   ")
    assert response.status_code == 422


# --- Ownership ---------------------------------------------------------


def test_ownership_returns_404_across_users(
    client: TestClient, db_session: Session
) -> None:
    owner_token = _make_premium_user(client, "owner@example.com")
    other_token = _make_premium_user(client, "other@example.com")

    playlist_id = _create_playlist(client, owner_token, "Privada").json()["id"]
    user_id = _get_user_id(db_session, "owner@example.com")
    song = _create_song_directly(db_session, user_id, "ready")
    client.post(
        f"/playlists/{playlist_id}/songs",
        json={"song_id": song.id},
        headers=_headers(owner_token),
    )

    other_headers = _headers(other_token)
    get_response = client.get(f"/playlists/{playlist_id}", headers=other_headers)
    assert get_response.status_code == 404
    assert (
        client.patch(
            f"/playlists/{playlist_id}", json={"name": "x"}, headers=other_headers
        ).status_code
        == 404
    )
    assert (
        client.delete(f"/playlists/{playlist_id}", headers=other_headers).status_code
        == 404
    )
    assert (
        client.post(
            f"/playlists/{playlist_id}/songs",
            json={"song_id": song.id},
            headers=other_headers,
        ).status_code
        == 404
    )
    assert (
        client.delete(
            f"/playlists/{playlist_id}/songs/{song.id}", headers=other_headers
        ).status_code
        == 404
    )
    assert (
        client.put(
            f"/playlists/{playlist_id}/songs/order",
            json={"song_ids": [song.id]},
            headers=other_headers,
        ).status_code
        == 404
    )


# --- Añadir canción ------------------------------------------------------


def test_add_song_happy_path_sets_position_and_added_at(
    client: TestClient, db_session: Session
) -> None:
    access_token = _make_premium_user(client, "addsong@example.com")
    playlist_id = _create_playlist(client, access_token).json()["id"]
    user_id = _get_user_id(db_session, "addsong@example.com")
    song = _create_song_directly(db_session, user_id, "ready")

    response = client.post(
        f"/playlists/{playlist_id}/songs",
        json={"song_id": song.id},
        headers=_headers(access_token),
    )
    assert response.status_code == 201
    songs = response.json()["songs"]
    assert len(songs) == 1
    assert songs[0]["position"] == 0
    assert songs[0]["added_at"] is not None


def test_add_song_nonexistent_returns_404(client: TestClient) -> None:
    access_token = _make_premium_user(client, "addmissing@example.com")
    playlist_id = _create_playlist(client, access_token).json()["id"]

    response = client.post(
        f"/playlists/{playlist_id}/songs",
        json={"song_id": 999999},
        headers=_headers(access_token),
    )
    assert response.status_code == 404


def test_add_song_not_ready_returns_409(
    client: TestClient, db_session: Session
) -> None:
    access_token = _make_premium_user(client, "addnotready@example.com")
    playlist_id = _create_playlist(client, access_token).json()["id"]
    user_id = _get_user_id(db_session, "addnotready@example.com")
    processing_song = _create_song_directly(db_session, user_id, "processing")

    response = client.post(
        f"/playlists/{playlist_id}/songs",
        json={"song_id": processing_song.id},
        headers=_headers(access_token),
    )
    assert response.status_code == 409


def test_add_song_duplicate_returns_409(
    client: TestClient, db_session: Session
) -> None:
    access_token = _make_premium_user(client, "adddup@example.com")
    playlist_id = _create_playlist(client, access_token).json()["id"]
    user_id = _get_user_id(db_session, "adddup@example.com")
    song = _create_song_directly(db_session, user_id, "ready")

    first = client.post(
        f"/playlists/{playlist_id}/songs",
        json={"song_id": song.id},
        headers=_headers(access_token),
    )
    assert first.status_code == 201

    duplicate = client.post(
        f"/playlists/{playlist_id}/songs",
        json={"song_id": song.id},
        headers=_headers(access_token),
    )
    assert duplicate.status_code == 409


def test_add_song_over_soft_cap_returns_409(
    client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("app.api.playlists._MAX_SONGS_PER_PLAYLIST", 1)
    access_token = _make_premium_user(client, "addcap@example.com")
    playlist_id = _create_playlist(client, access_token).json()["id"]
    user_id = _get_user_id(db_session, "addcap@example.com")
    song_a = _create_song_directly(db_session, user_id, "ready")
    song_b = _create_song_directly(db_session, user_id, "ready")

    ok = client.post(
        f"/playlists/{playlist_id}/songs",
        json={"song_id": song_a.id},
        headers=_headers(access_token),
    )
    assert ok.status_code == 201

    over_cap = client.post(
        f"/playlists/{playlist_id}/songs",
        json={"song_id": song_b.id},
        headers=_headers(access_token),
    )
    assert over_cap.status_code == 409


def test_create_playlist_over_soft_cap_returns_409(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("app.api.playlists._MAX_PLAYLISTS_PER_USER", 1)
    access_token = _make_premium_user(client, "playlistcap@example.com")

    ok = _create_playlist(client, access_token, "Primera")
    assert ok.status_code == 201

    over_cap = _create_playlist(client, access_token, "Segunda")
    assert over_cap.status_code == 409


# --- Quitar canción ------------------------------------------------------


def test_remove_song_renumbers_remaining_contiguously(
    client: TestClient, db_session: Session
) -> None:
    access_token = _make_premium_user(client, "removesong@example.com")
    playlist_id = _create_playlist(client, access_token).json()["id"]
    user_id = _get_user_id(db_session, "removesong@example.com")
    songs = [_create_song_directly(db_session, user_id, "ready") for _ in range(3)]
    for song in songs:
        client.post(
            f"/playlists/{playlist_id}/songs",
            json={"song_id": song.id},
            headers=_headers(access_token),
        )

    # Quita la canción del medio (position=1).
    response = client.delete(
        f"/playlists/{playlist_id}/songs/{songs[1].id}",
        headers=_headers(access_token),
    )
    assert response.status_code == 200
    remaining = response.json()["songs"]
    assert [s["song"]["id"] for s in remaining] == [songs[0].id, songs[2].id]
    assert [s["position"] for s in remaining] == [0, 1]


def test_remove_song_not_in_playlist_returns_404(client: TestClient) -> None:
    access_token = _make_premium_user(client, "removemissing@example.com")
    playlist_id = _create_playlist(client, access_token).json()["id"]

    response = client.delete(
        f"/playlists/{playlist_id}/songs/999999", headers=_headers(access_token)
    )
    assert response.status_code == 404


# --- Reordenar -----------------------------------------------------------


def test_reorder_happy_path(client: TestClient, db_session: Session) -> None:
    access_token = _make_premium_user(client, "reorder@example.com")
    playlist_id = _create_playlist(client, access_token).json()["id"]
    user_id = _get_user_id(db_session, "reorder@example.com")
    songs = [_create_song_directly(db_session, user_id, "ready") for _ in range(3)]
    for song in songs:
        client.post(
            f"/playlists/{playlist_id}/songs",
            json={"song_id": song.id},
            headers=_headers(access_token),
        )

    new_order = [songs[2].id, songs[0].id, songs[1].id]
    response = client.put(
        f"/playlists/{playlist_id}/songs/order",
        json={"song_ids": new_order},
        headers=_headers(access_token),
    )
    assert response.status_code == 200
    result_order = [s["song"]["id"] for s in response.json()["songs"]]
    assert result_order == new_order


def test_reorder_rejects_duplicate_in_payload(
    client: TestClient, db_session: Session
) -> None:
    """Un array con un id repetido (ej. [a, b, b]) tiene el mismo `set` que
    la playlist real {a, b} pero distinta longitud/multiset - debe
    rechazarse con 400, no aceptarse silenciosamente (hallazgo de la
    revisión: comparar solo por set no detecta esto)."""
    access_token = _make_premium_user(client, "reorderdup@example.com")
    playlist_id = _create_playlist(client, access_token).json()["id"]
    user_id = _get_user_id(db_session, "reorderdup@example.com")
    song_a = _create_song_directly(db_session, user_id, "ready")
    song_b = _create_song_directly(db_session, user_id, "ready")
    for song in (song_a, song_b):
        client.post(
            f"/playlists/{playlist_id}/songs",
            json={"song_id": song.id},
            headers=_headers(access_token),
        )

    response = client.put(
        f"/playlists/{playlist_id}/songs/order",
        json={"song_ids": [song_a.id, song_b.id, song_b.id]},
        headers=_headers(access_token),
    )
    assert response.status_code == 400


def test_reorder_rejects_mismatched_song_set(
    client: TestClient, db_session: Session
) -> None:
    access_token = _make_premium_user(client, "reordermismatch@example.com")
    playlist_id = _create_playlist(client, access_token).json()["id"]
    user_id = _get_user_id(db_session, "reordermismatch@example.com")
    song = _create_song_directly(db_session, user_id, "ready")
    client.post(
        f"/playlists/{playlist_id}/songs",
        json={"song_id": song.id},
        headers=_headers(access_token),
    )

    response = client.put(
        f"/playlists/{playlist_id}/songs/order",
        json={"song_ids": [999999]},
        headers=_headers(access_token),
    )
    assert response.status_code == 400


# --- Rate limiter ----------------------------------------------------------


def test_playlists_use_general_rate_limit_tier(client: TestClient) -> None:
    access_token = _make_premium_user(client, "ratelimit@example.com")
    response = client.get("/playlists", headers=_headers(access_token))
    assert response.headers["X-RateLimit-Limit"] == "60"
