# CIDI — Rate Limiter, Autenticador y "Spotify" API

[![CI](https://github.com/davidmorgadocarames/cidi_ratelimiter_autenticator_spotify/actions/workflows/ci.yml/badge.svg)](https://github.com/davidmorgadocarames/cidi_ratelimiter_autenticator_spotify/actions/workflows/ci.yml)

Repositorio: https://github.com/davidmorgadocarames/cidi_ratelimiter_autenticator_spotify

Proyecto de portfolio que combina, sobre una API tipo Spotify con datos reales:

- **CI/CD real** con GitHub Actions (tests, lint, type-check, despliegue a staging/producción).
- **Rate limiting con Redis** usando el algoritmo *token bucket*.
- **Autenticación de dos factores (TOTP, RFC 6238)** para proteger acciones sensibles.
- **API de streaming de audio**: subida y transcodificación, streaming con Range Requests,
  búsqueda, recomendaciones precalculadas, caché de contenido popular y sincronización entre
  dispositivos.

El objetivo es demostrar decisiones de arquitectura de backend a nivel de producción (no solo
"que funcione"), documentando explícitamente qué está implementado de verdad y qué queda como
diseño teórico razonado.

## Estado actual

🚧 **Fases 0-10 completadas** — scaffolding del proyecto, API base con autenticación JWT
(registro, login, refresh con rotación y detección de reuso, logout, endpoint `/auth/me`) contra
Postgres real, una UI mínima integrada (registro, login, dashboard) servida por la propia API,
autenticación de dos factores (TOTP, RFC 6238) protegiendo la activación de premium, tooling de
calidad de código local (mypy en modo `--strict`, ruff, black, cobertura, pre-commit), CI real en
GitHub Actions (tests + lint + type-check en cada PR, matriz de Python 3.10/3.11/3.12), rate
limiting con Redis (token bucket) protegiendo login/registro/2FA contra fuerza bruta, la API
completamente dockerizada (imagen multi-stage, no-root, healthchecks cruzados con
postgres/redis, smoke test en CI), subida/transcodificación de audio (`ffmpeg` + MinIO) con un
catálogo público de canciones, streaming real con Range Requests vía URLs firmadas de MinIO
(control plane/data plane), y búsqueda de canciones por título/artista con Meilisearch. Ver el
roadmap completo abajo y el detalle de qué está implementado vs pendiente en
[`docs/architecture.md`](docs/architecture.md).

## Frontend: UI integrada vs frontend separado

Para la Fase 2 se evaluó levantar un frontend separado (ej. Vite + JS/React con su propio dev
server) frente a servir una UI mínima directamente desde FastAPI. Se eligió la **UI integrada**:
HTML/CSS/JS vanilla en [`app/static/`](app/static/), servido como estáticos por la propia API
(`StaticFiles` montado en `/`), mismo origen — sin CORS, sin build tooling, sin dependencias de
frontend. Motivo: este es un proyecto de portfolio **backend-focused** (CI/CD, rate limiter,
TOTP, streaming); un frontend separado añadiría complejidad (configuración de CORS, otro conjunto
de dependencias, un proceso de build) sin aportar a lo que el proyecto quiere demostrar. La UI
solo necesita ser suficiente para probar el login, el registro y el toggle de premium de verdad
desde un navegador, no ser vistosa.

Uso: con la API corriendo, abre `http://localhost:8000/` — permite registrarte o iniciar sesión,
configurar 2FA (QR + confirmación) y, desde el dashboard, ver tu email, tu estado de premium y
activarlo/desactivarlo. Activar premium llama de verdad a
`POST /users/me/premium/activate` con contraseña + código TOTP (ver sección "Flujo de 2FA" abajo)
— no es un cambio solo visual; desactivar sigue siendo `POST /users/me/premium/deactivate`, sin
esa fricción extra. El access token vive únicamente en una variable JS en memoria (no en
`localStorage`, ver [`docs/architecture.md`](docs/architecture.md) para el matiz de qué protege
esto y qué no) — se pierde al recargar la página, pero un "silent refresh"
(`POST /auth/refresh` vía la cookie `httpOnly`) recupera la sesión automáticamente sin pedir
credenciales de nuevo.

## Flujo de 2FA y activación de premium

El código de 6 dígitos se calcula con [pyotp](https://github.com/pyauth/pyotp) (RFC 6238: HMAC-SHA1
sobre un contador de pasos de 30s, truncado dinámicamente a 6 dígitos), con una ventana de
tolerancia de ±1 paso (±30s) por posible desfase de reloj entre servidor y dispositivo. El secreto
se guarda cifrado en Postgres (Fernet) y nunca se vuelve a mostrar en claro tras el setup inicial.

```mermaid
sequenceDiagram
    actor Usuario
    participant Frontend
    participant API
    participant DB

    Usuario->>Frontend: Login (email + password)
    Frontend->>API: POST /auth/login
    API->>DB: Verificar credenciales
    API-->>Frontend: access_token + cookie refresh (httpOnly)

    Usuario->>Frontend: Configurar 2FA
    Frontend->>API: POST /2fa/setup (password)
    API->>DB: Verificar password, guardar secreto cifrado (Fernet)
    API-->>Frontend: QR + secreto (otpauth URI)
    Frontend-->>Usuario: Mostrar QR

    Usuario->>Frontend: Código de la app autenticadora
    Frontend->>API: POST /2fa/verify (code)
    API->>DB: Verificar código (±30s), marcar totp_confirmed_at
    API-->>Frontend: 2FA activado

    Usuario->>Frontend: Activar premium
    Frontend->>API: POST /users/me/premium/activate (password + code)
    alt 2FA no confirmado
        API-->>Frontend: 403 Configura 2FA antes de activar premium
    else 2FA confirmado
        API->>DB: Verificar lockout, password, código TOTP
        API->>DB: is_premium = true
        API-->>Frontend: 200 Premium activado
        Frontend-->>Usuario: "Premium activo"
    end
```

## Rate limiting con Redis (token bucket)

`POST /auth/login`, `POST /auth/register` y `POST /2fa/setup` (los únicos endpoints sin ninguna
otra protección propia contra fuerza bruta/credential stuffing) están limitados a 5 requests por
identidad con refill de 1 cada 12s; el resto de `/auth/*`, `/users/*` y `/2fa/*` tiene un límite
general de 60/min. La identidad es el `user_id` del access token si la request va autenticada, o
la IP del cliente si no (login/registro no tienen usuario todavía). Al superarse el límite, la API
responde `429` con cabeceras `X-RateLimit-Limit`/`X-RateLimit-Remaining`/`X-RateLimit-Reset`.

**Por qué Redis y no un contador en memoria del proceso**: un diccionario Python en memoria solo
sirve mientras haya un único proceso sirviendo requests. En cuanto la API corre con más de un
worker de `uvicorn`/`gunicorn`, o más de una réplica del contenedor (lo normal en producción; la
Fase 7 dockerizó la API pero con un único contenedor — réplicas reales detrás de un balanceador
quedan para una fase futura, ver Fase 9/15), cada proceso tendría su propio contador aislado — un
atacante repartiendo requests entre workers vería un límite efectivo multiplicado por el número de
procesos, no el límite real. Redis centraliza el estado del bucket en un único lugar que todos los
procesos comparten, independientemente de cuántos haya.

**Por qué token bucket y no un contador de ventana fija**: un contador simple ("máximo N requests
por minuto natural") permite ráfagas de hasta 2N requests pegadas al borde entre dos minutos (N al
final de uno, N al principio del siguiente). El token bucket con refill continuo no tiene ese
borde: la capacidad se recupera de forma gradual, no de golpe cada minuto.

El estado de cada bucket (tokens disponibles, última vez que se rellenó) se lee, calcula y escribe
en un único script Lua ejecutado atómicamente por Redis (`EVAL`), evitando que dos requests
concurrentes de la misma identidad puedan leer el mismo valor antes de que ninguna escriba (el
mismo tipo de *lost update* que ya apareció y se corrigió en Postgres para la rotación de refresh
tokens y el lockout de TOTP). Detalle completo del diseño, los dos tiers y el trade-off de
fail-open en [`docs/architecture.md`](docs/architecture.md).

## Subida y transcodificación de audio (Fase 8)

`POST /songs` (multipart: `title`, `artist`, `file`) sube un archivo de audio, lo valida de verdad
con `ffprobe` (nunca confiando en el `Content-Type` que declara el cliente — fácilmente
falseable), lo transcodifica a mp3 192kbps con `ffmpeg`, y guarda tanto el original como el
transcodificado en MinIO (compatible S3, vía `boto3`). Todo síncrono dentro del propio request —
sin cola de tareas todavía (Celery es Fase 11) — así que una subida grande mantiene ocupado un
hilo del servidor mientras dura. `GET /songs`/`GET /songs/{id}` exponen el catálogo, público para
cualquier usuario autenticado (no solo el que subió cada canción) — más fiel a un clon real de
Spotify que un catálogo privado por usuario. Sin endpoint de borrado — ver Fase 9 abajo para cómo
se sirve el audio de verdad. Decisiones completas, límites (20MB por archivo) y riesgos aceptados
(agotamiento del threadpool compartido bajo subidas concurrentes, filas atascadas si el proceso
muere a mitad) en [`docs/architecture.md`](docs/architecture.md).

## Streaming de audio (Fase 9)

`GET /songs/{id}/stream` no sirve audio directamente — devuelve una URL de MinIO firmada
(SigV2/SigV4 según el endpoint, ver `docs/architecture.md`) válida 15 minutos. Es el "control
plane" de la separación que promete el roadmap: FastAPI solo autoriza (`Authorization: Bearer`
obligatorio) y firma; MinIO ("data plane") sirve los bytes directo, con soporte nativo de Range
Requests (`206 Partial Content`) sin que la API tenga que reimplementarlo. Un `<audio src="...">`
HTML no puede mandar headers custom, así que la URL YA lleva su propia autorización embebida — el
flujo real es JS pidiendo la URL con `fetch()` autenticado y luego asignándola a `audio.src`, sin
que el streaming en sí pase nunca por el rate limiter ni por FastAPI. Siempre se firma el
transcodificado, nunca el original. Sin UI de reproducción en esta fase (decisión explícita,
backend-focused) — verificado con requests HTTP reales (`httpx`) contra la URL firmada, incluyendo
Range Requests reales y expiración real (no simulada). Decisiones y riesgos completos en
[`docs/architecture.md`](docs/architecture.md).

## Búsqueda (Fase 10)

`GET /songs/search?q=...&limit=&offset=` busca por título/artista con [Meilisearch](https://www.meilisearch.com/),
endpoint dedicado y separado de `GET /songs` (mismo principio de separación de responsabilidades ya
aplicado a `storage.py`/`transcode.py`). Cada canción se indexa en cuanto termina de subirse y
transcodificarse con éxito (`status="ready"`) — la indexación es **best-effort**: si Meilisearch
falla o no responde, la subida sigue devolviendo `201` igual (MinIO/ffmpeg ya hicieron su trabajo,
la canción es reproducible; la búsqueda es una capa derivada, no el núcleo de la funcionalidad,
mismo principio de fail-open que Redis en la Fase 6). Al buscar, en cambio, un Meilisearch
inalcanzable **sí** se refleja explícitamente: `503`, nunca una lista vacía engañosa. El endpoint
construye la respuesta directamente desde los documentos que devuelve Meilisearch, sin volver a
consultar Postgres (válido mientras no exista edición/borrado de canciones — ver
`docs/architecture.md`). Canciones subidas *antes* de esta fase no quedan indexadas retroactivamente
(sin backfill en el alcance actual). Decisiones completas en
[`docs/architecture.md`](docs/architecture.md).

## Stack técnico (previsto)

- **Backend**: Python 3 + FastAPI
- **Base de datos**: PostgreSQL (SQLAlchemy 2.0)
- **Cache / Rate limiting**: Redis
- **Búsqueda**: Meilisearch
- **Cola de tareas en background**: Celery + Redis
- **Almacenamiento de objetos**: MinIO (compatible S3)
- **Contenedores**: Docker / docker-compose
- **CI/CD**: GitHub Actions

## Roadmap

| Fase | Contenido |
| ---- | --------- |
| 0 | Scaffolding del proyecto |
| 1 | API base y autenticación (JWT) |
| 2 | UI de login y registro, toggle de premium |
| 3 | Autenticación de dos factores (TOTP) |
| 4 | Calidad de código local (pytest-cov, ruff, black, mypy, pre-commit) |
| 5 | CI (GitHub Actions, matriz de Python) |
| 6 | Rate limiter con Redis (token bucket) |
| 7 | Contenedores (Docker, docker-compose, healthchecks) |
| 8 | Subida y transcodificación de audio (ffmpeg, MinIO) |
| 9 | Streaming de audio (Range Requests, control plane vs data plane) |
| 10 | Búsqueda (Meilisearch) |
| 11 | Recomendaciones precalculadas (Celery) |
| 12 | Caché de contenido popular |
| 13 | Sincronización entre dispositivos |
| 14 | CD (staging/producción, rollback) |
| 15 | Documentación final para portfolio |

## Desarrollo local

Requiere Python 3.13 en local (única versión instalada en esta máquina). El CI (Fase 5) corre
además la suite completa contra 3.10/3.11/3.12 en GitHub Actions — verificado en verde en las
tres, ver [`docs/architecture.md`](docs/architecture.md#riesgos-conocidos). Requiere también
Docker (para Postgres, adelantado a la Fase 1; Redis, adelantado a la Fase 6; MinIO, Fase 8; y
Meilisearch, Fase 10), y `ffmpeg`/`ffprobe` instalados en el sistema (Fase 8,
subida/transcodificación de audio — **no** es un paquete pip, es un binario de sistema; en
Windows: `winget install --id Gyan.FFmpeg -e`, verificar con `ffmpeg -version`; dentro de Docker
ya viene instalado en la imagen, ver Dockerfile).

```bash
python -m venv .venv
# Windows (PowerShell)
.venv\Scripts\Activate.ps1
# Linux/Mac
source .venv/bin/activate

# Para desarrollo (ya incluye requirements.txt vía -r; añade pytest, httpx, pytest-cov,
# ruff, black, mypy, pre-commit):
pip install -r requirements-dev.txt
# Si solo necesitas runtime (lo que instala la imagen Docker de producción,
# Fase 7 - ver Dockerfile), sin nada de lo anterior:
pip install -r requirements.txt
```

Copia `.env.example` a `.env` y completa los valores. Desde la Fase 1 son obligatorias:
`DATABASE_URL`, `TEST_DATABASE_URL`, `JWT_SECRET_KEY` (la app rechaza arrancar con el valor
placeholder `change-me` — genera uno real con `openssl rand -hex 32`) y `COOKIE_SECURE` (`false`
en desarrollo local sin HTTPS). `REDIS_URL` (Fase 6) no es estrictamente obligatoria — tiene
un valor por defecto (`redis://localhost:6379/0`) sin validador de placeholder, a diferencia de
`JWT_SECRET_KEY` — pero hay que asegurarse de que apunte al Redis real que se levante.
`S3_ACCESS_KEY`/`S3_SECRET_KEY` (Fase 8) sí tienen el mismo validador anti-`change-me` que
`JWT_SECRET_KEY` — deben coincidir con `MINIO_ROOT_USER`/`MINIO_ROOT_PASSWORD` del servicio
`minio` de `docker-compose.yml` (que los lee del propio `.env`, ver esa sección).
`MEILISEARCH_API_KEY` (Fase 10) tiene el mismo validador anti-`change-me` y debe coincidir con
`MEILI_MASTER_KEY` del servicio `meilisearch`, mismo mecanismo. El resto de variables se van
sumando fase a fase.

Levantar Postgres, Redis, MinIO y Meilisearch, y aplicar las migraciones, antes de correr la API o
los tests:

```bash
docker compose up -d postgres redis minio meilisearch
alembic upgrade head
uvicorn app.main:app --reload
# en otra terminal
pytest tests/ -v
```

### Todo en Docker (Fase 7)

Alternativa al flujo de arriba, no su reemplazo — el venv local sigue siendo lo más rápido para
iterar con `--reload`; esto sirve para probar el camino de despliegue real (imagen de producción)
en vez del de desarrollo, o para no necesitar Python instalado en absoluto. Requiere el mismo
`.env` con secretos reales de la sección anterior (`JWT_SECRET_KEY`/`TOTP_ENCRYPTION_KEY`, no el
placeholder `change-me`) — el `ENTRYPOINT` migra al arrancar y esa migración ya importa
`app.core.config`, que valida esos secretos aunque no los use directamente. **Nota**: el servicio
`app` publica el puerto 8000, igual que `uvicorn --reload` en el flujo de arriba — no corras ambos
a la vez, o el segundo falla con "address already in use".

```bash
docker compose up -d --build   # postgres + redis + minio + meilisearch + la API, las 5 healthy antes de servir
curl http://localhost:8000/health
```

La imagen (`Dockerfile`, multi-stage) instala solo `requirements.txt` — nunca
`requirements-dev.txt` — corre como usuario no-root, y su `ENTRYPOINT`
(`docker/entrypoint.sh`) aplica `alembic upgrade head` antes de arrancar `uvicorn`, así que no
hace falta el paso manual de migraciones. `depends_on: condition: service_healthy` sobre
postgres/redis significa que la API no arranca hasta que ambos estén realmente `healthy`, no solo
iniciados. Detalle completo de las decisiones (por qué multi-stage, por qué el usuario no-root, el
riesgo aceptado de migraciones automáticas con múltiples réplicas) en
[`docs/architecture.md`](docs/architecture.md).

Los tests corren contra una base de datos Postgres real y separada (`spotify_clone_test`, creada
automáticamente por `docker/postgres-init/`), no contra SQLite ni con mocks.

## Calidad de código local

Con `requirements-dev.txt` instalado:

```bash
ruff check .                              # linter
black .                                   # formateador
mypy app tests                            # chequeo de tipos (--strict)
pytest tests/ --cov=app --cov-report=term-missing   # tests + cobertura
```

`pre-commit install` (una vez) activa un hook de git que corre automáticamente `ruff`, `black`,
`mypy` y unos hooks de higiene básicos (espacios en blanco, fin de archivo, YAML válido) antes de
cada commit. **pytest no está en este hook a propósito**: necesita Postgres corriendo en Docker,
así que se deja para CI (Fase 5) y para correrlo manualmente. El hook de `mypy` es local
(`language: system`, no el mirror oficial de pre-commit) porque resolver los tipos de
FastAPI/SQLAlchemy/Pydantic correctamente requiere el entorno del proyecto instalado — esto
significa que usa el `mypy` que encuentre en el `PATH` de quien commitea: **el venv debe estar
activado** en la terminal donde se hace `git commit`, o el hook falla con
`Executable 'mypy' not found`.

## CI (GitHub Actions)

`.github/workflows/ci.yml` corre en cada Pull Request contra `main` (y en cada push a `main`,
como red de seguridad post-merge): las mismas herramientas de la sección anterior
(`ruff check`, `black --check`, `mypy app tests`, `pytest --cov=app --cov-fail-under=85`) más
`alembic upgrade head` contra un Postgres real y un Redis real (Fase 6), ambos levantados como
servicios del propio job — ahora obligatorias en cada PR, no solo un hook local. Matriz de Python
`3.10`, `3.11`, `3.12` (el mínimo soportado según se documentó desde la Fase 0, no la 3.13 del
entorno local).

**Setup necesario en GitHub antes del primer run** (una sola vez):

1. **Secrets** — Settings → Secrets and variables → Actions → New repository secret. El workflow
   necesita `JWT_SECRET_KEY`/`TOTP_ENCRYPTION_KEY` válidos (la app rechaza arrancar con el
   placeholder `change-me`), pero son valores **efímeros de CI, no credenciales reales** — nunca
   hardcodeados en el YAML por higiene (ver `docs/architecture.md`), sino como Secrets:
   - `CI_JWT_SECRET_KEY`
   - `CI_TOTP_ENCRYPTION_KEY`
2. **Branch protection** — Settings → Branches → Add branch protection rule → branch name
   pattern `main` → marcar "Require status checks to pass before merging" → seleccionar los 3
   checks de la matriz (`test (3.10)`, `test (3.11)`, `test (3.12)`) una vez hayan corrido al
   menos una vez (aparecen en la lista tras el primer PR). Esto bloquea el merge si el CI falla.

## Decisiones de diseño

Documentadas progresivamente en [`docs/architecture.md`](docs/architecture.md) a medida que se
toman (por qué Redis, por qué token bucket, por qué TOTP, por qué Postgres, etc.).

## Licencia y autoría

MIT License. Proyecto personal de portfolio desarrollado por [davidmorgadocarames](https://github.com/davidmorgadocarames).
