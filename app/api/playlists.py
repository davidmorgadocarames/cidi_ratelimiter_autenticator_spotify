from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.auth import get_current_user
from app.db.session import get_db
from app.models.playlist import Playlist, PlaylistSong
from app.models.song import Song
from app.models.user import User
from app.schemas.playlist import (
    PlaylistCreate,
    PlaylistDetail,
    PlaylistRead,
    PlaylistReorderRequest,
    PlaylistSongCreate,
    PlaylistSongRead,
    PlaylistUpdate,
)
from app.schemas.song import SongRead

# Límites blandos, altos a propósito - no deben notarse en uso normal, solo
# evitan el caso patológico del renumerado O(N) por mutación (ver
# app/models/playlist.py) degradándose sin límite.
_MAX_PLAYLISTS_PER_USER = 100
_MAX_SONGS_PER_PLAYLIST = 500


def _require_premium_and_totp(current_user: User = Depends(get_current_user)) -> User:
    """Lee is_premium/totp_enabled del User recién cargado de DB por
    get_current_user (no claims del token) - revocar premium tiene efecto
    inmediato en la siguiente request. Declarada a nivel de router (ver
    `dependencies=` de abajo), no endpoint por endpoint - así es
    estructuralmente imposible que una ruta nueva nazca sin el gate."""
    if not (current_user.is_premium and current_user.totp_enabled):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Esta función requiere premium y 2FA activados",
        )
    return current_user


router = APIRouter(
    prefix="/playlists",
    tags=["playlists"],
    dependencies=[Depends(_require_premium_and_totp)],
)


def _get_owned_playlist(
    db: Session, playlist_id: int, user_id: int, *, lock: bool = False
) -> Playlist:
    """404 (no 403) si la playlist no existe o es de otro usuario - no
    confirma existencia de IDs ajenos, mismo idioma ya usado en songs.py.
    lock=True añade FOR UPDATE sobre esta fila para serializar mutaciones
    concurrentes de las PlaylistSong de la misma playlist (add/remove/
    reorder) - mismo patrón ya usado en app/api/totp.py para el lockout."""
    query = select(Playlist).where(
        Playlist.id == playlist_id, Playlist.user_id == user_id
    )
    if lock:
        query = query.with_for_update()
    playlist = db.scalar(query)
    if playlist is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Playlist no encontrada"
        )
    return playlist


def _load_playlist_detail(db: Session, playlist: Playlist) -> PlaylistDetail:
    """song_count/total_duration_seconds se calculan en Python a partir de
    las mismas filas ya cargadas para `songs` - evita una query agregada
    aparte (y con ella, el bug clásico de LEFT JOIN + count(*)/sum() dando
    1/NULL en vez de 0/0.0 para una playlist vacía)."""
    rows = db.execute(
        select(PlaylistSong, Song)
        .join(Song, Song.id == PlaylistSong.song_id)
        .where(PlaylistSong.playlist_id == playlist.id)
        .order_by(PlaylistSong.position)
    ).all()
    songs = [
        PlaylistSongRead(
            song=SongRead.model_validate(song),
            position=playlist_song.position,
            added_at=playlist_song.added_at,
        )
        for playlist_song, song in rows
    ]
    total_duration = sum((item.song.duration_seconds or 0.0) for item in songs)
    return PlaylistDetail(
        id=playlist.id,
        name=playlist.name,
        created_at=playlist.created_at,
        song_count=len(songs),
        total_duration_seconds=total_duration,
        songs=songs,
    )


def _renumber(ordered_rows: list[PlaylistSong]) -> None:
    for position, row in enumerate(ordered_rows):
        row.position = position


@router.post("", response_model=PlaylistRead, status_code=status.HTTP_201_CREATED)
def create_playlist(
    payload: PlaylistCreate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> PlaylistRead:
    existing_count = db.scalar(
        select(func.count())
        .select_from(Playlist)
        .where(Playlist.user_id == current_user.id)
    )
    if existing_count is not None and existing_count >= _MAX_PLAYLISTS_PER_USER:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Límite de {_MAX_PLAYLISTS_PER_USER} playlists alcanzado",
        )

    playlist = Playlist(user_id=current_user.id, name=payload.name)
    db.add(playlist)
    db.commit()
    db.refresh(playlist)
    return PlaylistRead(
        id=playlist.id,
        name=playlist.name,
        created_at=playlist.created_at,
        song_count=0,
        total_duration_seconds=0.0,
    )


@router.get("", response_model=list[PlaylistRead])
def list_playlists(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[PlaylistRead]:
    """Una única query agregada con GROUP BY para todas las playlists del
    usuario, no N llamadas a _load_playlist_detail - evita N+1. func.count
    sobre PlaylistSong.id (no count(*)) + coalesce(sum(...), 0.0): con
    outerjoin, una playlist vacía produce una fila con columnas NULL, y
    count(*) contaría esa fila igual (dando 1, no 0) - count sobre una
    columna cuenta solo valores no-nulos."""
    rows = db.execute(
        select(
            Playlist.id,
            Playlist.name,
            Playlist.created_at,
            func.count(PlaylistSong.id).label("song_count"),
            func.coalesce(func.sum(Song.duration_seconds), 0.0).label(
                "total_duration_seconds"
            ),
        )
        .outerjoin(PlaylistSong, PlaylistSong.playlist_id == Playlist.id)
        .outerjoin(Song, Song.id == PlaylistSong.song_id)
        .where(Playlist.user_id == current_user.id)
        .group_by(Playlist.id)
        .order_by(Playlist.created_at.desc())
    ).all()
    return [
        PlaylistRead(
            id=row.id,
            name=row.name,
            created_at=row.created_at,
            song_count=row.song_count,
            total_duration_seconds=row.total_duration_seconds,
        )
        for row in rows
    ]


@router.get("/{playlist_id}", response_model=PlaylistDetail)
def get_playlist(
    playlist_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> PlaylistDetail:
    playlist = _get_owned_playlist(db, playlist_id, current_user.id)
    return _load_playlist_detail(db, playlist)


@router.patch("/{playlist_id}", response_model=PlaylistRead)
def rename_playlist(
    playlist_id: int,
    payload: PlaylistUpdate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> PlaylistRead:
    playlist = _get_owned_playlist(db, playlist_id, current_user.id)
    playlist.name = payload.name
    db.commit()
    detail = _load_playlist_detail(db, playlist)
    return PlaylistRead(
        id=detail.id,
        name=detail.name,
        created_at=detail.created_at,
        song_count=detail.song_count,
        total_duration_seconds=detail.total_duration_seconds,
    )


@router.delete("/{playlist_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_playlist(
    playlist_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> None:
    playlist = _get_owned_playlist(db, playlist_id, current_user.id)
    db.delete(playlist)
    db.commit()


@router.post(
    "/{playlist_id}/songs",
    response_model=PlaylistDetail,
    status_code=status.HTTP_201_CREATED,
)
def add_song_to_playlist(
    playlist_id: int,
    payload: PlaylistSongCreate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> PlaylistDetail:
    playlist = _get_owned_playlist(db, playlist_id, current_user.id, lock=True)

    song = db.get(Song, payload.song_id)
    if song is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Canción no encontrada"
        )
    if song.status != "ready":
        # Whitelist (!= "ready"), mismo patrón y mensajes ya usados en
        # get_song_stream_url (app/api/songs.py) - una canción no reproducible
        # no tiene por qué ser añadible a una playlist pensada para reproducirse.
        detail = (
            "Todavía se está procesando"
            if song.status == "processing"
            else "El procesamiento de esta canción falló"
        )
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=detail)

    current_count = db.scalar(
        select(func.count())
        .select_from(PlaylistSong)
        .where(PlaylistSong.playlist_id == playlist.id)
    )
    if current_count is not None and current_count >= _MAX_SONGS_PER_PLAYLIST:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Límite de {_MAX_SONGS_PER_PLAYLIST} canciones alcanzado",
        )

    playlist_song = PlaylistSong(
        playlist_id=playlist.id, song_id=song.id, position=current_count or 0
    )
    db.add(playlist_song)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="La canción ya está en esta playlist",
        ) from None

    return _load_playlist_detail(db, playlist)


@router.delete("/{playlist_id}/songs/{song_id}", response_model=PlaylistDetail)
def remove_song_from_playlist(
    playlist_id: int,
    song_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> PlaylistDetail:
    playlist = _get_owned_playlist(db, playlist_id, current_user.id, lock=True)

    playlist_song = db.scalar(
        select(PlaylistSong).where(
            PlaylistSong.playlist_id == playlist.id, PlaylistSong.song_id == song_id
        )
    )
    if playlist_song is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Esa canción no está en esta playlist",
        )
    db.delete(playlist_song)
    db.flush()

    remaining_rows = list(
        db.scalars(
            select(PlaylistSong)
            .where(PlaylistSong.playlist_id == playlist.id)
            .order_by(PlaylistSong.position)
        )
    )
    _renumber(remaining_rows)
    db.commit()

    return _load_playlist_detail(db, playlist)


@router.put("/{playlist_id}/songs/order", response_model=PlaylistDetail)
def reorder_playlist_songs(
    playlist_id: int,
    payload: PlaylistReorderRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> PlaylistDetail:
    playlist = _get_owned_playlist(db, playlist_id, current_user.id, lock=True)

    existing_rows = list(
        db.scalars(select(PlaylistSong).where(PlaylistSong.playlist_id == playlist.id))
    )
    rows_by_song_id = {row.song_id: row for row in existing_rows}
    existing_song_ids = list(rows_by_song_id.keys())

    # Validación por MULTISET, no por set: comparar solo como conjuntos deja
    # pasar duplicados en el payload (ej. [1,2,3,3] contra una playlist
    # {1,2,3}) - el set coincidiría pero una fila recibiría dos posiciones
    # distintas en el renumerado. len() + sorted() detecta duplicados,
    # faltantes y ids ajenos/no pertenecientes a esta playlist por igual.
    if len(payload.song_ids) != len(existing_song_ids) or sorted(
        payload.song_ids
    ) != sorted(existing_song_ids):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="La lista de canciones no coincide con las de la playlist",
        )

    ordered_rows = [rows_by_song_id[song_id] for song_id in payload.song_ids]
    _renumber(ordered_rows)
    db.commit()

    return _load_playlist_detail(db, playlist)
