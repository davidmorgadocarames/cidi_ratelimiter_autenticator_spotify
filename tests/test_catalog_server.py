from pathlib import Path

from fastapi.testclient import TestClient

from app.cli.catalog_server import create_app

_HEADER = "track_name,artists,track_genre\n"


def _write_csv(tmp_path: Path, rows: list[str]) -> Path:
    csv_path = tmp_path / "tracks.csv"
    csv_path.write_text(_HEADER + "".join(rows), encoding="utf-8")
    return csv_path


def test_tracks_endpoint_maps_columns_and_paginates(tmp_path: Path) -> None:
    rows = [f"Song {i},Artist {i},pop\n" for i in range(5)]
    csv_path = _write_csv(tmp_path, rows)
    client = TestClient(create_app(str(csv_path)))

    response = client.get("/tracks", params={"offset": 0, "limit": 2})

    assert response.status_code == 200
    assert response.json() == [
        {"title": "Song 0", "artist": "Artist 0"},
        {"title": "Song 1", "artist": "Artist 1"},
    ]


def test_tracks_endpoint_offset_beyond_data_returns_empty(tmp_path: Path) -> None:
    csv_path = _write_csv(tmp_path, ["Song 0,Artist 0,pop\n"])
    client = TestClient(create_app(str(csv_path)))

    response = client.get("/tracks", params={"offset": 100, "limit": 10})

    assert response.status_code == 200
    assert response.json() == []


def test_rows_missing_title_or_artist_are_dropped(tmp_path: Path) -> None:
    csv_path = _write_csv(
        tmp_path,
        [
            "Song 0,Artist 0,pop\n",
            ",Artist 1,pop\n",
            "Song 2,,pop\n",
            "Song 3,Artist 3,pop\n",
        ],
    )
    client = TestClient(create_app(str(csv_path)))

    response = client.get("/tracks", params={"offset": 0, "limit": 10})

    assert response.json() == [
        {"title": "Song 0", "artist": "Artist 0"},
        {"title": "Song 3", "artist": "Artist 3"},
    ]


def test_limit_rejects_values_above_500(tmp_path: Path) -> None:
    csv_path = _write_csv(tmp_path, ["Song 0,Artist 0,pop\n"])
    client = TestClient(create_app(str(csv_path)))

    response = client.get("/tracks", params={"limit": 501})

    assert response.status_code == 422
