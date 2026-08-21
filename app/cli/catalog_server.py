"""Servidor HTTP mínimo, aislado del resto de la app, que sirve el catálogo de
un CSV de Kaggle (título/artista) en memoria - simula "traer el catálogo de un
servidor" para app/cli/seed_catalog.py, en vez de leer el CSV directamente.

Nunca se importa desde app/main.py ni ningún código de producción. Se lanza a
mano dentro del contenedor "app" (ver docs/architecture.md, sección de seed de
catálogo):

    python -m app.cli.catalog_server --csv /tmp/spotify_tracks.csv --port 8899
"""

import argparse
import csv
from dataclasses import dataclass

from fastapi import FastAPI, Query
from pydantic import BaseModel

# Columnas reales del dataset de Kaggle "Spotify Tracks Dataset"
# (maharshipandya/spotify-tracks-dataset). Si el CSV descargado trae otro
# header, ajustar estas dos constantes.
_TITLE_COLUMN = "track_name"
_ARTIST_COLUMN = "artists"


@dataclass
class TrackRecord:
    title: str
    artist: str


class TrackOut(BaseModel):
    title: str
    artist: str


def load_tracks(csv_path: str) -> list[TrackRecord]:
    """Lee el CSV entero a memoria, descartando filas sin título o artista."""
    tracks: list[TrackRecord] = []
    with open(csv_path, encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            title = (row.get(_TITLE_COLUMN) or "").strip()
            artist = (row.get(_ARTIST_COLUMN) or "").strip()
            if not title or not artist:
                continue
            tracks.append(TrackRecord(title=title, artist=artist))
    return tracks


def create_app(csv_path: str) -> FastAPI:
    app = FastAPI(title="Catalog server (seed de Kaggle, no es la app real)")
    tracks = load_tracks(csv_path)

    @app.get("/tracks", response_model=list[TrackOut])
    def get_tracks(
        offset: int = Query(0, ge=0),
        limit: int = Query(100, ge=1, le=500),
    ) -> list[TrackRecord]:
        return tracks[offset : offset + limit]

    return app


def main() -> None:  # pragma: no cover
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--csv", required=True, help="Ruta al CSV de Kaggle")
    # 127.0.0.1, no 0.0.0.0: docker-compose.yml no declara una red propia, así
    # que otros servicios del compose (celery-worker/celery-beat) resuelven
    # "app" por DNS y podrían alcanzar este servidor transitorio si escuchara
    # en todas las interfaces. seed_catalog.py lo consulta por localhost
    # dentro del mismo contenedor, así que 127.0.0.1 no rompe nada.
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8899)
    args = parser.parse_args()

    import uvicorn

    uvicorn.run(create_app(args.csv), host=args.host, port=args.port)


if __name__ == "__main__":  # pragma: no cover
    main()
