import threading
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
import uvicorn
from mypy_boto3_s3.client import S3Client
from sqlalchemy.orm import Session, sessionmaker

from app.cli.catalog_server import create_app
from app.cli.seed_catalog import (
    TrackRecord,
    ensure_synthetic_audio,
    fetch_tracks,
    main,
    seed_tracks,
)
from app.core.config import settings
from app.core.security import hash_password
from app.models.song import Song
from app.models.user import User

PASSWORD = "supersecret"


def _create_user(db: Session, email: str) -> int:
    user = User(email=email, hashed_password=hash_password(PASSWORD))
    db.add(user)
    db.commit()
    db.refresh(user)
    return user.id


@pytest.fixture()
def running_catalog_server(tmp_path: Path) -> Iterator[str]:
    """Arranca app/cli/catalog_server.py de verdad (uvicorn en un hilo de
    fondo, puerto efímero) contra un CSV pequeño de fixture - así
    fetch_tracks() se testea contra HTTP real, no mockeado."""
    csv_path = tmp_path / "tracks.csv"
    csv_path.write_text(
        "track_name,artists,track_genre\n"
        + "".join(f"Song {i},Artist {i},pop\n" for i in range(5)),
        encoding="utf-8",
    )
    config = uvicorn.Config(
        create_app(str(csv_path)), host="127.0.0.1", port=0, log_level="warning"
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    while not server.started:
        time.sleep(0.01)
    port = server.servers[0].sockets[0].getsockname()[1]
    try:
        yield f"http://127.0.0.1:{port}/tracks"
    finally:
        server.should_exit = True
        thread.join(timeout=5)


def test_fetch_tracks_paginates_over_real_http(running_catalog_server: str) -> None:
    tracks = fetch_tracks(running_catalog_server, limit=3)

    assert [t.title for t in tracks] == ["Song 0", "Song 1", "Song 2"]


def test_fetch_tracks_stops_when_source_is_exhausted(
    running_catalog_server: str,
) -> None:
    tracks = fetch_tracks(running_catalog_server, limit=100)

    assert len(tracks) == 5


def test_ensure_synthetic_audio_uploads_to_minio(s3_client: S3Client) -> None:
    key, duration_seconds, file_size_bytes = ensure_synthetic_audio(
        tone_duration_seconds=1.0
    )

    assert key == "seed/kaggle-synthetic-tone.mp3"
    assert duration_seconds == 1.0
    assert file_size_bytes > 0
    s3_client.head_object(Bucket=settings.s3_bucket_name, Key=key)


def test_seed_tracks_creates_songs_and_is_idempotent(db_session: Session) -> None:
    user_id = _create_user(db_session, "seed-user@example.com")
    tracks = [
        TrackRecord(title="Song A", artist="Artist A"),
        TrackRecord(title="Song B", artist="Artist B"),
    ]

    created, skipped, not_indexed = seed_tracks(
        db_session, tracks, user_id, "seed/tone.mp3", 30.0, 12345
    )
    assert (created, skipped, not_indexed) == (2, 0, 0)

    songs = db_session.query(Song).order_by(Song.title).all()
    assert [s.title for s in songs] == ["Song A", "Song B"]
    for song in songs:
        assert song.status == "ready"
        assert song.original_object_key == "seed/tone.mp3"
        assert song.transcoded_object_key == "seed/tone.mp3"
        assert song.duration_seconds == 30.0

    # Segunda pasada con los mismos tracks: no debe duplicar filas.
    created_again, skipped_again, not_indexed_again = seed_tracks(
        db_session, tracks, user_id, "seed/tone.mp3", 30.0, 12345
    )
    assert (created_again, skipped_again, not_indexed_again) == (0, 2, 0)
    assert db_session.query(Song).count() == 2


def test_seed_tracks_truncates_long_title_and_artist(db_session: Session) -> None:
    user_id = _create_user(db_session, "seed-user-2@example.com")
    long_title = "T" * 300
    long_artist = "A" * 300

    created, _skipped, _not_indexed = seed_tracks(
        db_session,
        [TrackRecord(title=long_title, artist=long_artist)],
        user_id,
        "seed/tone.mp3",
        30.0,
        12345,
    )

    assert created == 1
    song = db_session.query(Song).one()
    assert len(song.title) == 255
    assert len(song.artist) == 255


def test_main_end_to_end_seeds_and_is_idempotent(
    running_catalog_server: str,
    db_session: Session,
    s3_client: S3Client,
    test_session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("app.cli.seed_catalog.SessionLocal", test_session_factory)
    email = "seed-main-user@example.com"
    _create_user(db_session, email)

    main(
        [
            "--user-email",
            email,
            "--source-url",
            running_catalog_server,
            "--limit",
            "3",
            "--tone-duration",
            "1",
        ]
    )

    assert db_session.query(Song).count() == 3

    # Segunda pasada: idempotente, no duplica.
    main(
        [
            "--user-email",
            email,
            "--source-url",
            running_catalog_server,
            "--limit",
            "3",
            "--tone-duration",
            "1",
        ]
    )
    assert db_session.query(Song).count() == 3


def test_main_exits_with_error_for_unknown_user(
    running_catalog_server: str,
    test_session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("app.cli.seed_catalog.SessionLocal", test_session_factory)

    with pytest.raises(SystemExit) as exc_info:
        main(
            [
                "--user-email",
                "no-existe@example.com",
                "--source-url",
                running_catalog_server,
                "--limit",
                "3",
            ]
        )

    assert exc_info.value.code == 1


def test_main_rejects_limit_above_max(
    test_session_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("app.cli.seed_catalog.SessionLocal", test_session_factory)

    with pytest.raises(SystemExit) as exc_info:
        main(["--user-email", "whoever@example.com", "--limit", "1001"])

    assert exc_info.value.code == 1


def test_main_exits_cleanly_when_catalog_server_is_unreachable(
    db_session: Session,
    test_session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Mismo escenario que motivó el hallazgo de la revisión: --source-url
    apuntando a un puerto sin nada escuchando debía dar un SystemExit(1) con
    mensaje claro, no un traceback crudo."""
    monkeypatch.setattr("app.cli.seed_catalog.SessionLocal", test_session_factory)
    email = "seed-unreachable@example.com"
    _create_user(db_session, email)

    with pytest.raises(SystemExit) as exc_info:
        main(
            [
                "--user-email",
                email,
                "--source-url",
                "http://127.0.0.1:1/tracks",  # puerto reservado, nada escucha ahí
                "--limit",
                "3",
            ]
        )

    assert exc_info.value.code == 1
