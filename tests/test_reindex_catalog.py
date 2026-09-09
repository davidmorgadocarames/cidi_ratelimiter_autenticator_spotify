import pytest
from sqlalchemy.orm import Session, sessionmaker

from app.cli.reindex_catalog import main, reindex_all
from app.core.security import hash_password
from app.models.song import Song
from app.models.user import User
from app.services import search

PASSWORD = "supersecret"


def _create_user(db: Session, email: str) -> int:
    user = User(email=email, hashed_password=hash_password(PASSWORD))
    db.add(user)
    db.commit()
    db.refresh(user)
    return user.id


def _create_song(
    db: Session, user_id: int, title: str, artist: str, status: str = "ready"
) -> Song:
    song = Song(
        title=title,
        artist=artist,
        uploaded_by_id=user_id,
        status=status,
        original_object_key=f"reindex-test/{title}",
        transcoded_object_key=f"reindex-test/{title}.mp3",
        duration_seconds=30.0,
        content_type="audio/mpeg",
        file_size_bytes=12345,
    )
    db.add(song)
    db.commit()
    db.refresh(song)
    return song


def test_reindex_all_reindexes_only_ready_songs(db_session: Session) -> None:
    user_id = _create_user(db_session, "reindex-user@example.com")
    _create_song(db_session, user_id, "Ready Song", "Artist A", status="ready")
    _create_song(
        db_session, user_id, "Processing Song", "Artist B", status="processing"
    )

    ok, fail = reindex_all(db_session)

    assert (ok, fail) == (1, 0)


def test_reindex_all_attempts_every_song_even_with_repeated_failures(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No debe abortar a mitad por una racha de fallos - datos malos en unas
    pocas filas no deben impedir reindexar el resto de un catálogo sano
    (hallazgo de la revisión: un fail-fast por "N fallos seguidos" confunde
    filas con datos corruptos con Meilisearch caído de verdad)."""
    user_id = _create_user(db_session, "reindex-fail-user@example.com")
    for i in range(5):
        _create_song(db_session, user_id, f"Song {i}", "Artist", status="ready")

    monkeypatch.setattr("app.cli.reindex_catalog.search.index_song", lambda song: False)

    ok, fail = reindex_all(db_session)

    assert (ok, fail) == (0, 5)


def test_main_end_to_end_reindexes_and_exits_cleanly(
    db_session: Session,
    test_session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("app.cli.reindex_catalog.SessionLocal", test_session_factory)
    user_id = _create_user(db_session, "reindex-main-user@example.com")
    _create_song(db_session, user_id, "Main Song", "Main Artist", status="ready")

    main()  # no debe lanzar SystemExit si no hay fallos

    hits = search.search_songs("Main Song", limit=10, offset=0)
    assert any(hit["title"] == "Main Song" for hit in hits)


def test_main_exits_with_error_when_indexing_fails(
    db_session: Session,
    test_session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("app.cli.reindex_catalog.SessionLocal", test_session_factory)
    monkeypatch.setattr("app.cli.reindex_catalog.search.index_song", lambda song: False)
    user_id = _create_user(db_session, "reindex-main-fail-user@example.com")
    _create_song(db_session, user_id, "Failing Song", "Artist", status="ready")

    with pytest.raises(SystemExit) as exc_info:
        main()

    assert exc_info.value.code == 1


def test_main_aborts_before_touching_db_when_meilisearch_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("app.cli.reindex_catalog.search.is_healthy", lambda: False)

    def _fail_if_called(db: Session) -> tuple[int, int]:
        raise AssertionError(
            "reindex_all no debería llamarse si Meilisearch está caído"
        )

    monkeypatch.setattr("app.cli.reindex_catalog.reindex_all", _fail_if_called)

    with pytest.raises(SystemExit) as exc_info:
        main()

    assert exc_info.value.code == 1
