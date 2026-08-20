# Arquitectura

> Este documento se construye de forma incremental, fase a fase. Cada sección se completa cuando
> la fase correspondiente del roadmap (ver `README.md`) se implementa.

## Resumen

API FastAPI con autenticación JWT (Fase 1). Endpoints en `app/api/auth.py`:

- `POST /auth/register` — crea usuario (409 si el email ya existe).
- `POST /auth/login` — valida credenciales, devuelve un access token JWT (bearer, ~30 min) en el
  body y setea el refresh token como cookie `httpOnly`.
- `POST /auth/refresh` — rota el refresh token (revoca el usado, emite uno nuevo); detecta reuso
  de un token ya rotado y revoca toda la sesión como medida de seguridad.
- `POST /auth/logout` — revoca el refresh token actual.
- `GET /auth/me` — devuelve el usuario autenticado (requiere `Authorization: Bearer <access_token>`).

Persistencia en Postgres vía SQLAlchemy 2.0 (`app/models/user.py`: `User`, `RefreshToken`),
migraciones con Alembic. 13 tests (`tests/test_auth.py`) corriendo contra una base de datos
Postgres real (no mocks/SQLite), incluyendo rotación, detección de reuso, expiración real y una
race condition de dos refresh simultáneos con el mismo token.

**Fase 2 — UI mínima + toggle de premium.** UI servida como estáticos en `/`
(`app/static/index.html`, `app.js`, `style.css`): tres vistas (cargando, auth con login+registro,
dashboard) en una sola página, sin framework ni build tooling. Endpoint de toggle de premium en
`app/api/users.py`, protegido con la misma dependencia `get_current_user` de Fase 1 (en Fase 3 se
reemplaza por `activate`/`deactivate`, ver abajo). Nueva columna `is_premium` en `User`
(migración `e43601f7d62c`). 8 tests nuevos (`tests/test_users.py`, `tests/test_main.py`) —
incluyen rechazo de payload inválido (422) y una regresión que verifica que el mount de estáticos
en `/` no tapa las rutas de `/auth/*` ni `/health`.

Verificación manual en navegador real (no solo tests): flujo completo conducido con Playwright
headless + capturas de pantalla — primera visita sin parpadeo, registro con auto-login, toggle de
premium con estado de carga correcto, recarga de página con silent refresh manteniendo la sesión
y el estado premium, logout, error de credenciales incorrectas, error de email duplicado con
link a login. Esta verificación encontró y permitió corregir un bug real que los tests
automatizados no habían cubierto: el botón de toggle mostraba el label equivocado tras un cambio
exitoso (ver `withLoading` en `app/static/app.js`).

**Fase 3 — 2FA (TOTP).** Nuevo router `app/api/totp.py` (`POST /2fa/setup`, `POST /2fa/verify`):
secreto generado con pyotp, cifrado con Fernet y guardado en `User.totp_secret_encrypted`;
confirmación con un código válido marca `totp_confirmed_at`. `PATCH /users/me/premium` de Fase 2
se **reemplaza** por `POST /users/me/premium/activate` (contraseña + código TOTP, exige 2FA ya
confirmado) y `POST /users/me/premium/deactivate` (simple, como antes) en `app/api/users.py`.
Lockout ad-hoc en Postgres (5 intentos fallidos → 15 min de bloqueo, columnas
`totp_failed_attempts`/`totp_locked_until`), compartido entre ambos endpoints que verifican un
código, protegido con `SELECT ... FOR UPDATE` (`lock_user_for_totp_check`) — sin esto, intentos
concurrentes pierden incrementos del contador y neutralizan el lockout, mismo patrón que la race
condition de refresh tokens en Fase 1. 21 tests nuevos (`tests/test_totp.py`,
`tests/test_users.py` reescrito) — código válido, incorrecto, dentro/fuera de la ventana de
tolerancia (con `counter_offset` de pyotp para que el borde del step sea determinista, no
segundos de reloj), doble setup (409), fallo controlado de descifrado, y una prueba de
concurrencia real (5 hilos, mismo token) que confirma que el lockout no se puede saltear.

Verificación manual: flujo completo con `curl` generando códigos reales vía pyotp, y en navegador
real con Playwright. Esa verificación encontró y corrigió un bug real de CSS: una clase
`.field-stack { display: flex }` tenía la misma especificidad que la regla `[hidden]` del
navegador y ganaba por ser CSS de autor, dejando visibles secciones de la UI que debían estar
ocultas (arreglado con `.field-stack:not([hidden])`). La ronda de revisión posterior encontró
además que la primera versión del fix de concurrencia (`SELECT ... FOR UPDATE` sin
`populate_existing=True`) bloqueaba la fila en Postgres correctamente pero SQLAlchemy seguía
devolviendo el objeto Python ya cacheado en el identity map de la sesión (con el contador
desactualizado) — el test de concurrencia lo detectó de inmediato.

**Fase 4 — Calidad de código local.** Config centralizada en `pyproject.toml` nuevo: ruff
(`E`, `F`, `I`, `UP`, `B`, `SIM`, `C4`), black, mypy en modo `--strict` completo, pytest y
coverage. `requirements-dev.txt` nuevo separa herramientas de desarrollo (pytest, httpx,
pytest-cov, ruff, black, mypy, pre-commit) de `requirements.txt` (solo runtime). `.pre-commit-config.yaml`
nuevo con hooks de higiene + ruff + black + mypy (hook local, no el mirror oficial — ver
Decisiones técnicas).

Corregir mypy `--strict` sobre las ~25 archivos ya escritos en Fases 1-3 fue, con diferencia, la
parte que más trabajo dio: 61 errores en la primera corrida, la gran mayoría (`no-untyped-def`)
en funciones de test sin `-> None` y fixtures de `conftest.py` sin tipar, más dos casos reales en
`app/` (`decode_access_token` devolviendo `dict` sin parámetros genéricos; `decrypt_totp_secret`
recibiendo un `str | None` sin narrowing en `app/api/users.py`, resuelto con un chequeo explícito
sobre la fila ya bloqueada, no solo para satisfacer al type-checker). Cero uso de `# type: ignore`
— no hizo falta ninguno. *(Corrección posterior, Fase 5: el resumen original decía que ni
`pyotp` ni `qrcode` necesitaban el override de `ignore_missing_imports` — impreciso. Verificado de
nuevo: `pyotp` sí viene tipado y no lo necesita, pero `qrcode` no publica stubs/`py.typed` y mypy
falla sin el override. El override para `qrcode.*` se mantiene en `pyproject.toml`, el de
`pyotp.*` se quitó por innecesario.)*

Cobertura real medida: **94%** (`pytest --cov=app`), sin umbral obligatorio todavía (ver
Decisiones técnicas). Los huecos son en su mayoría ramas de error poco alcanzables en tests
unitarios (`app/db/session.py` 69%, la construcción del engine/sesión en sí) y algunas líneas de
`app/api/auth.py`/`app/core/config.py`.

**Fase 5 — CI (GitHub Actions).** `.github/workflows/ci.yml` reemplaza el stub inerte de Fase 0:
corre en cada Pull Request contra `main` y en cada push a `main` (`concurrency` con
`cancel-in-progress` para no duplicar runs), matriz `python-version: ["3.10", "3.11", "3.12"]`,
servicio Postgres (`postgres:16-alpine`, mismas credenciales que `docker-compose.yml`). Mismos
pasos que el hook de pre-commit de Fase 4 más `alembic upgrade head` y `pytest --cov-fail-under=85`,
ahora obligatorios en cada PR, no solo en un hook local que depende de que el venv esté activado.

Toda la secuencia de comandos (crear `spotify_clone_test`, migrar, ruff, black, mypy, pytest con
`--cov-fail-under=85`) se verificó localmente contra Postgres real antes de pushear, incluyendo
una prueba aislada de la lógica de `CREATE DATABASE` (con `autocommit=True`, requisito de
Postgres para ese comando) contra una base de datos descartable, sin tocar el `spotify_clone_test`
real de desarrollo.

Este es el primer push del proyecto a GitHub (`origin/main` solo tenía el commit inicial de
scaffolding de agentes hasta ahora): se verificó explícitamente con
`git log --all --full-history -- .env` que `.env` nunca se trackeó en ninguno de los 5 commits de
Fases 0-4, antes de hacerlos públicos por primera vez.

**Fase 6 — Rate limiter con Redis (token bucket).** Nuevo `app/core/rate_limiter.py`:
`RateLimitMiddleware` (`BaseHTTPMiddleware` de Starlette, único middleware de la app, registrado
en `app/main.py`) que limita `POST /auth/login`, `POST /auth/register` y `POST /2fa/setup` (tier
`sensitive`: capacidad 5, refill 1/12s) y el resto de `/auth/*`, `/users/*`, `/2fa/*` (tier
`general`: capacidad 60, refill 1/s). Identidad: `user_id` del access token si la request va
autenticada (decodificado sin tocar la DB), IP del cliente si no. El estado del bucket
(`tokens`, `last_refill`) vive en un hash de Redis por identidad, leído/calculado/escrito
atómicamente en un único script Lua (`EVAL`) — evita el mismo tipo de *lost update* concurrente ya
corregido en Postgres en Fases 1 y 3. Al superarse el límite, `429` con cabeceras
`X-RateLimit-Limit`/`X-RateLimit-Remaining`/`X-RateLimit-Reset` (añadidas en toda respuesta que
pasa por el limiter y sí llega a evaluarse contra Redis, no solo el 429 — **excepto en fail-open**:
si Redis no responde, `dispatch` retorna antes de construir esas cabeceras, así que una respuesta
que pasó "gracias" al fail-open no las lleva). Si Redis no responde, la request pasa igual
(fail-open, ver Decisiones técnicas y Riesgos conocidos).

Nuevo servicio `redis` en `docker-compose.yml` y en `.github/workflows/ci.yml` (mismo patrón que
`postgres`). 8 tests (`tests/test_rate_limiter.py`) contra Redis real (`FLUSHDB` autouse en
`tests/conftest.py`, mismo patrón que el `TRUNCATE` de Postgres): cabeceras en requests permitidas,
bloqueo al superar los tiers `sensitive` y `general`, buckets independientes por identidad,
identidad por `user_id` y no por IP cuando hay token en un endpoint autenticado, un Bearer propio
adjuntado a `/auth/login` sigue cayendo en el bucket por IP (regresión del bug de bypass, ver
abajo), refill simulado manipulando `last_refill` directamente en Redis (sin esperar en tiempo
real), y fail-open verificado con una `RateLimitMiddleware` aislada apuntando a una URL Redis
inválida. Los tests de lockout de TOTP ya existentes
(`test_verify_lockout_after_five_failed_attempts`,
`test_verify_concurrent_wrong_attempts_lockout_not_bypassed`) se ampliaron para afirmar también el
`detail` del 429 (`"Inténtalo de nuevo en..."`), no solo el código de estado — demuestra que ese
429 lo dispara el lockout de TOTP y no el rate limiter genérico.

**Bug real #1, encontrado y corregido durante la implementación**: la primera versión usaba
`redis.asyncio.Redis`, creado una única vez en `RateLimitMiddleware.__init__` sobre el `app`
singleton. Ese cliente async queda ligado al event loop activo la primera vez que se usa; en los
tests, cada `TestClient(app)` (fixture `client`, sin `with` explícito) puede acabar corriendo la
app ASGI en un event loop distinto, dejando la conexión Redis cacheada huérfana y lanzando
`RuntimeError: Event loop is closed` en requests posteriores — excepción que el propio `except
Exception` de fail-open tragaba en silencio, convirtiendo 429 esperados en 401 inesperados en los
tests. Diagnosticado como riesgo real de arquitectura (no solo un artefacto de test: cualquier
escenario con más de un event loop activo en el proceso lo dispararía), corregido cambiando a un
cliente `redis.Redis` **síncrono** despachado vía `starlette.concurrency.run_in_threadpool`, sin
afinidad a ningún event loop.

**Bug real #2, encontrado en la ronda de revisión Agent Teams posterior a la implementación** (por
Security y por el abogado del diablo, de forma independiente): `_resolve_identity` usaba
`user:<sub>` siempre que hubiera un Bearer válido, sin importar el endpoint. Pero `/auth/login` y
`/auth/register` no usan ese header para autenticar — lo ignoran por completo. Un atacante podía
adjuntar un access token de una cuenta propia a sus intentos de login contra una víctima y así
mover ese tráfico del bucket `ip:<atacante>` a un bucket `user:<atacante>` aparte; registrando N
cuentas conseguía N buckets `sensitive` independientes contra el mismo endpoint, amplificando por
N el límite que ese tier existe para imponer — precisamente el vector de fuerza bruta que esta
fase debía cerrar, y en contradicción directa con el diseño documentado ("por IP del cliente" para
login/registro). Corregido forzando identidad por IP (`force_ip=True`, ignorando cualquier Bearer)
específicamente para `POST /auth/login` y `POST /auth/register` (`_IP_ONLY_ENDPOINTS` en
`app/core/rate_limiter.py`) — `/2fa/setup`, el tercer endpoint `sensitive`, sí exige autenticación
real para llegar al handler, así que ahí bucketear por `user_id` sigue siendo correcto y
deliberado. Cubierto por
`tests/test_rate_limiter.py::test_login_with_bearer_token_still_rate_limited_by_ip`.

**Corrección adicional de la misma ronda de revisión** (Security y Backend, de forma
independiente): el `except Exception` de fail-open envolvía también el parseo del resultado del
script Lua (`int(result[0])`, `float(result[1])`), no solo la llamada a Redis. Un bug de
programación propio en esa ruta (ej. un cambio futuro en lo que devuelve el Lua) se habría
enmascarado como "Redis caído" y degradado en silencio a fail-open, en vez de fallar de forma
visible. Corregido estrechando el `except` a `redis.RedisError` únicamente alrededor de la llamada
a Redis (`run_in_threadpool`); el parseo, fuera del `try`, ahora sí propagaría como error real si
algo cambiara ahí.

**Fase 7 — Contenedores (Docker, docker-compose, healthchecks).** Nuevo `Dockerfile`
(multi-stage): etapa `builder` instala solo `requirements.txt` (nunca `requirements-dev.txt`) en
un virtualenv en `/opt/venv`; etapa final copia únicamente ese venv ya construido + `app/` +
`alembic.ini`, corre como usuario no-root (`useradd --system`), con `HEALTHCHECK` propio (un
one-liner de Python contra `GET /health`, sin depender de `curl`). `ENTRYPOINT` nuevo
(`docker/entrypoint.sh`) aplica `alembic upgrade head` antes de `exec`utar el `CMD`
(`uvicorn app.main:app`), así que el contenedor migra solo al arrancar. Nuevo servicio `app` en
`docker-compose.yml`, con `depends_on: condition: service_healthy` sobre `postgres`/`redis` — el
"healthcheck cruzado" que se lleva prometiendo desde la Fase 1 — y `DATABASE_URL`/`REDIS_URL`
sobreescritas a los hostnames internos de compose (`postgres`/`redis`) en vez de `localhost`.
Nuevo job `docker` en CI (`needs: test`): construye la imagen y la arranca de verdad contra
postgres/redis reales de CI (`--network host`), esperando `GET /health` con reintentos antes de
darlo por bueno — un smoke test real, no solo "el Dockerfile tiene sintaxis válida". **Matiz
importante encontrado en la ronda de revisión post-implementación (abogado del diablo)**:
`GET /health` es un chequeo de *liveness* puro (`app/main.py`) — no toca Postgres ni Redis en
runtime, solo demuestra que el proceso de `uvicorn` está vivo. La conexión a Postgres sí queda
probada indirectamente (el `ENTRYPOINT` migra antes de arrancar; si Postgres fallara, el contenedor
ni llegaría a servir `/health`), pero Redis no se ejercitaba en absoluto — y al ser el rate limiter
fail-open (Fase 6), un `REDIS_URL` roto habría pasado el smoke test igual, en silencio. Corregido
añadiendo un segundo paso que hace `POST /auth/register` y exige la cabecera `X-RateLimit-Limit`
en la respuesta — esa cabecera solo se añade en el camino feliz del rate limiter (Redis respondió
con éxito), nunca en fail-open, así que su ausencia expone un Redis inalcanzable que `/health` por
sí solo no habría detectado.

Verificación manual (no solo automatizada): `docker compose up -d --build` con los tres servicios
`healthy`, y el flujo completo registro → login → `/2fa/setup` → `/2fa/verify` → activar premium →
`GET /auth/me` probado por primera vez contra la imagen de producción real, no contra el proceso
de desarrollo local de siempre. También se verificó explícitamente que `pip list` dentro de la
imagen no incluye pytest/ruff/black/mypy, y que el proceso corre como usuario no-root.

**Bug real encontrado durante la propia verificación de esta fase** (no en la ronda de revisión de
Agent Teams, sino al ejecutar los comandos de verificación del plan): `docker run --rm <imagen>
pip list` y `docker run --rm <imagen> whoami` fallaban silenciosamente, porque `docker run <imagen>
<comando>` sustituye el `CMD`, no el `ENTRYPOINT` — `docker/entrypoint.sh` seguía ejecutándose
primero e intentando `alembic upgrade head`, que requiere `JWT_SECRET_KEY`/`TOTP_ENCRYPTION_KEY`
válidas (import eager de `app.core.config.settings` desde `env.py`, acoplamiento preexistente de
Fase 1: instanciar `Settings()` valida TODAS las variables, no solo las que usa cada comando
concreto) para poder siquiera importar el módulo. Sin esas variables (no se pasaron en esos
`docker run` sueltos de introspección), el contenedor fallaba con un `ValidationError` antes de
llegar a `pip list`/`whoami`. Además, el primer intento de verificar esto con un `grep` sobre la
salida enmascaró el fallo real como si fuera un "OK" (el patrón buscado —
`pytest|ruff|black|mypy`— no aparecía en el traceback de error, así que el `|| echo "OK"` de la
propia verificación se disparó por la razón equivocada). Corregido usando
`docker run --rm --entrypoint pip/whoami <imagen> ...` para esos chequeos de introspección
(que no necesitan migrar nada), re-verificado con salida real esta vez. No es un bug del
`Dockerfile` en sí — el camino real (`docker compose up`, con todas las variables presentes vía
`env_file`) siempre funcionó — pero sí una lección sobre cómo `ENTRYPOINT` interactúa con `docker
run <comando>` y sobre no confiar en un pase de verificación cuyo propio mecanismo de comprobación
(`grep` + `||`) puede enmascarar el fallo que se supone que debía detectar.

## Diagrama de arquitectura

_Pendiente — diagrama completo (Mermaid o imagen) planificado para la Fase 15._

## Fases implementadas vs diseño teórico

Esta tabla distingue qué partes del proyecto están realmente implementadas y funcionando, y
cuáles quedan como diseño documentado (explicado pero no construido, por alcance/tiempo de un
proyecto de portfolio).

| Fase | Descripción                              | Estado        |
| ---- | ----------------------------------------- | ------------- |
| 0    | Scaffolding del proyecto                  | ✅ Implementado |
| 1    | API base y autenticación (JWT)            | ✅ Implementado |
| 2    | UI de login y toggle de premium           | ✅ Implementado |
| 3    | 2FA (TOTP)                                | ✅ Implementado |
| 4    | Calidad de código local                   | ✅ Implementado |
| 5    | CI                                        | ✅ Implementado |
| 6    | Rate limiter con Redis                    | ✅ Implementado |
| 7    | Contenedores                              | ✅ Implementado |
| 8    | Subida y transcodificación de audio       | ⬜ Pendiente    |
| 9    | Streaming de audio                        | ⬜ Pendiente    |
| 10   | Búsqueda                                  | ⬜ Pendiente    |
| 11   | Recomendaciones precalculadas (Celery)    | ⬜ Pendiente    |
| 12   | Caché de contenido popular                | ⬜ Pendiente    |
| 13   | Sincronización entre dispositivos         | ⬜ Pendiente    |
| 14   | CD                                        | ⬜ Pendiente    |
| 15   | Documentación final                       | ⬜ Pendiente    |

## Decisiones técnicas

_Se documentará cada decisión (por qué Redis, por qué token bucket, por qué TOTP, etc.) en la
fase en que se toma. Por ahora, las de la Fase 1:_

**Fase 1 — API base y autenticación**

- **SQLAlchemy 2.0 (estilo `Mapped`/declarative) en vez de SQLModel**: separa explícitamente el
  modelo de persistencia (`app/models/`) de los schemas de validación de API (`app/schemas/`),
  patrón más cercano a cómo se estructura un backend real de producción.
- **Refresh token persistido y revocable en Postgres, no JWT stateless**: permite un `logout`
  real que invalida la sesión en el servidor, no solo en el cliente.
- **Rotación del refresh token en cada `/auth/refresh` + detección de reuso**: cada uso de un
  refresh token lo revoca y emite uno nuevo (misma `family_id`). Si se reutiliza un token ya
  revocado, se asume robo y se revoca toda la `family_id` (patrón usado por Auth0/Okta). Esto
  significa que dos requests concurrentes con el mismo token (doble pestaña, retry de red)
  invalidan la sesión completa como medida de seguridad — es el comportamiento esperado, no un
  bug (protegido con `SELECT ... FOR UPDATE` para que sea determinista bajo concurrencia real; ver
  `tests/test_auth.py::test_refresh_concurrent_requests_only_one_succeeds`).
- **Refresh token en cookie `httpOnly` + `Secure` (configurable) + `SameSite=Lax`; access token
  JWT en el body JSON**: el refresh token no es accesible desde JavaScript (mitiga XSS). El access
  token vive en memoria en el cliente. `SameSite=Lax` es la mitigación de CSRF aceptada para este
  proyecto — no es CSRF-proof al 100% (no protege ante subdominios comprometidos ni sustituye un
  token CSRF dedicado), pero es un compromiso razonable para el alcance de un portfolio.
- **SHA-256 para `token_hash`, no bcrypt**: el refresh token ya es aleatorio de alta entropía
  (`secrets.token_urlsafe(32)`); bcrypt está pensado para contraseñas de baja entropía, trunca a
  72 bytes y su salt variable impide indexar por hash. SHA-256 permite lookup indexado directo.
- **bcrypt directo para contraseñas, sin `passlib`**: `passlib[bcrypt]` tiene un bug de
  compatibilidad conocido con `bcrypt>=4.1` y el proyecto está prácticamente sin mantenimiento.
- **PyJWT en vez de `python-jose`**: más activamente mantenido.
- **psycopg3 (`psycopg[binary]`) en vez de psycopg2**: driver moderno recomendado hoy por
  SQLAlchemy.
- **Alembic desde la Fase 1**: evita tener que migrar "a mano" un esquema ya creado con
  `create_all()` más adelante.
- **Postgres adelantado a la Fase 1** (en vez de esperar a la Fase 7): la Fase 1 necesita una base
  de datos real para el modelo de usuario; se adelanta solo el contenedor de Postgres en
  `docker-compose.yml`, el resto del stack completo (API dockerizada, healthchecks cruzados) queda
  para la Fase 7 — **implementado en Fase 7**, ver esa sección.
- **Mitigación de timing side-channel en `/auth/login`**: cuando el email no existe, se ejecuta
  igualmente un hash bcrypt contra un valor "dummy" para que la respuesta tarde lo mismo que con
  una contraseña incorrecta, evitando enumerar emails registrados por diferencia de tiempo.
- **`JWT_SECRET_KEY` no puede quedar en su valor placeholder**: `Settings` rechaza arrancar si
  sigue en `"change-me"` (falla rápido en vez de firmar tokens con un secreto público conocido).

**Fase 2 — UI mínima y toggle de premium**

- **UI mínima integrada en FastAPI, no frontend separado**: HTML/CSS/JS vanilla servido como
  estáticos por la propia API (mismo origen, sin CORS, sin build tooling). Justificación completa
  en el README (sección "Frontend: UI integrada vs frontend separado") — el foco de este proyecto
  es backend, no frontend.
- **Formulario de registro incluido en la UI** (no solo login): permite probar el flujo completo
  desde el navegador sin pasar antes por curl/pytest.
- **"Silent refresh" al cargar la página**: `POST /auth/refresh` (la cookie httpOnly viaja sola)
  al iniciar, para no perder la sesión en cada recarga — el access token vive solo en una variable
  JS en memoria, no en `localStorage`.
- **PATCH con valor explícito `is_premium`, no un "toggle puro"**: evita que un reintento de red
  invierta el estado dos veces sin que el usuario lo note.
- **Matiz sobre "access token en memoria, no localStorage"**: reduce el robo *pasivo* (una
  extensión de navegador maliciosa u otro script leyendo `localStorage`), pero **no** protege
  frente a un XSS activo en la propia página — con ejecución de JS arbitraria ahí, un atacante no
  necesita leer la variable: puede llamar él mismo a `POST /auth/refresh` (la cookie se envía
  igual; `httpOnly` bloquea la *lectura* por JS, no el *envío* por el navegador) o interceptar
  `fetch`/`XMLHttpRequest` para capturar el header `Authorization` en la próxima llamada real. No
  se presenta como mitigación de XSS, solo de robo pasivo de credenciales en reposo.

**Fase 3 — 2FA (TOTP)**

- **pyotp, no implementación manual del algoritmo**: aunque el enunciado permitía cualquiera de
  las dos, pyotp usa `hmac.compare_digest` internamente (verificado leyendo su código fuente, no
  asumido) — comparación en tiempo constante, resistente a timing attacks, algo fácil de hacer
  mal en una implementación propia de código de seguridad.
- **Fernet (`cryptography`) para cifrar el secreto TOTP en reposo**: cifrado simétrico
  autenticado. `TOTP_ENCRYPTION_KEY` se valida al arranque con el mismo patrón que
  `JWT_SECRET_KEY` — rechaza el placeholder `change-me` y además valida que sea una key Fernet
  bien formada, para fallar rápido en vez de reventar en el primer login con 2FA.
- **Ventana de tolerancia `valid_window=1` (±30s)**: por posible desfase de reloj entre servidor
  y dispositivo, tal como pide el enunciado.
- **Lockout ad-hoc en Postgres (5 intentos fallidos → 15 min de bloqueo), implementado ya en esta
  fase, no pospuesto a Fase 6**: un código de 6 dígitos es un espacio de búsqueda pequeño y fijo
  (10^6, ampliado a 3 códigos válidos simultáneos por la ventana de tolerancia) — sin límite es
  fuerza bruta realmente explotable, no un riesgo teórico (así lo señaló la revisión de seguridad
  del plan antes de implementar). Compartido entre `/2fa/verify` y la verificación TOTP de
  `/users/me/premium/activate` — es la misma prueba de posesión del dispositivo. Protegido con
  `SELECT ... FOR UPDATE` + `populate_existing=True` para que sea correcto bajo concurrencia real
  (ver detalle en el Resumen de esta fase).
- **`POST /2fa/setup` exige contraseña**, no solo un access token válido: por simetría con
  `/premium/activate`, y porque sin esto un access token robado (30 min, riesgo ya documentado en
  Fase 1) permitiría configurar 2FA sobre la cuenta de la víctima sin conocer su contraseña.
- **Reemplazo de `PATCH /users/me/premium` (Fase 2) por `activate`/`deactivate` explícitos**: un
  solo endpoint con requisitos distintos según la dirección del cambio (activar exige
  password+TOTP, desactivar no exige nada extra) es peor diseño que dos endpoints explícitos.
- **Activar premium exige 2FA ya confirmado** (403 si no): la protección contraseña+TOTP no tiene
  sentido si el usuario nunca configuró el segundo factor: se le pide configurarlo primero en vez
  de permitir una activación "más débil" sin él.

**Fase 4 — Calidad de código local**

- **mypy en modo `--strict` completo**, no un subconjunto: decisión explícita del usuario,
  asumiendo el coste de corregir el código ya escrito (ver Resumen) a cambio de mantener ese
  rigor en las fases siguientes desde el primer commit de cada una, en vez de ir acumulando deuda
  de tipado para corregir toda de golpe más adelante.
- **`requirements-dev.txt` separado de `requirements.txt`**: las herramientas de Fase 4 (pytest,
  ruff, black, mypy, pre-commit) son de desarrollo, no de runtime — relevante de cara a la Fase 7,
  donde la imagen Docker de producción no necesita ninguna de ellas instalada.
- **pytest fuera de pre-commit**: necesita Postgres corriendo en Docker; atarlo a cada commit
  sería frágil (falla si Docker no está arriba) y cada vez más lento a medida que crece la suite.
  Queda para CI (Fase 5) y para correrlo manualmente.
- **`mypy` como hook local de pre-commit** (`language: system`) en vez del mirror oficial:
  resolver los tipos de FastAPI/SQLAlchemy/Pydantic correctamente requiere el entorno real del
  proyecto instalado. Trade-off aceptado: el hook depende de que el venv esté activado en la
  terminal donde se commitea (documentado en el README), a cambio de no duplicar a mano la lista
  de dependencias en `additional_dependencies`.
- **`pytest-cov` sin `--cov-fail-under` todavía**: se mide la cobertura real (94%, ver Resumen)
  antes de comprometerse a un umbral — fijar uno arbitrario sin conocer el número real podría
  bloquear commits legítimos o quedar puesto artificialmente bajo. Se revisa en Fase 5.

**Fase 5 — CI (GitHub Actions)**

- **`--cov-fail-under=85`**, no el 94% real medido: deja margen razonable para fluctuaciones
  normales fase a fase sin que el CI se ponga rojo por una caída pequeña y esperable, pero sí
  bloquea una regresión real y significativa de cobertura en un PR futuro.
- **DB de test creada en un paso explícito con `psycopg`, no vía bind-mount de
  `docker/postgres-init/`**: los service containers de GitHub Actions arrancan antes de que
  exista el checkout del repo, así que el mecanismo que funciona en local/docker-compose no es
  aplicable en CI. `CREATE DATABASE` se ejecuta con `autocommit=True` explícito (Postgres no
  permite ese comando dentro de una transacción) contra la base `spotify_clone` (que sí existe
  desde el arranque del servicio, vía `POSTGRES_DB`), no contra `spotify_clone_test` (que aún no
  existe en ese momento).
- **`JWT_SECRET_KEY`/`TOTP_ENCRYPTION_KEY` como GitHub Secrets, no hardcodeados en el YAML**:
  aunque son valores efímeros de CI sin ningún dato real detrás, la regla de "nunca secretos en
  un repo público, ni falsos" evita por completo el riesgo de que alguien copie ese valor "que ya
  funciona" a un `.env` real más adelante por conveniencia.
- **Branch protection configurada manualmente por el usuario, no por mí**: no hay `gh` CLI
  instalada en el entorno de desarrollo de este proyecto; instrucciones paso a paso en el README
  en vez de intentarlo por la API REST directamente con curl y un token.
- **Rama + PR real para esta fase, no commit directo a `main`**: a diferencia de las Fases 0-4
  (commiteadas directo a `main`), esta se hizo vía PR para que el workflow de CI corriera de
  verdad contra la matriz de Python antes de mergear — la primera demostración real de que
  funciona, no solo de que la sintaxis del YAML es válida.

**Fase 6 — Rate limiter con Redis**

- **Token bucket, no ventana fija**: refill continuo evita el efecto de ráfaga doble en el borde
  entre dos ventanas (hasta 2N requests pegadas al minuto natural con un contador simple). Detalle
  y comparación en el README (sección "Rate limiting con Redis").
- **Script Lua atómico (`EVAL`), no varias llamadas Redis separadas**: lectura + cálculo de refill
  + decremento + escritura en un único paso serializado por Redis, mismo motivo que
  `SELECT ... FOR UPDATE` en Postgres (Fases 1 y 3) — sin esto, dos requests concurrentes de la
  misma identidad podrían leer el mismo `tokens` antes de que ninguna escriba.
- **Dos tiers (`sensitive`/`general`), no un límite uniforme**: un único límite no protege de
  verdad login/registro/2FA-setup (habría que ponerlo muy bajo, molestando el uso normal de la
  API) ni tiene sentido ser igual de estricto en endpoints que no son objetivo de fuerza bruta.
- **`/2fa/verify` y `/users/me/premium/activate` en el tier `general`, no `sensitive`, pese a ser
  sensibles**: ya tienen el lockout de TOTP de la Fase 3 (5 intentos → 15 min en Postgres), más
  estricto que cualquier tier de este rate limiter. Aplicarles además el tier `sensitive` haría
  que el 429 del middleware (segundos de reset, mensaje genérico) se disparase antes que el 429
  del lockout (minutos de reset, mensaje específico) — dos relojes y mensajes distintos según cuál
  gane la carrera. Con capacidad 60 en `general`, el lockout (dispara a los 5 intentos) siempre
  actúa primero.
- **`/auth/refresh` en `general`, no `sensitive`**: el refresh token es aleatorio de 256 bits
  (`secrets.token_urlsafe(32)`), no adivinable por fuerza bruta bajo ningún límite de rate
  razonable — su protección real es la entropía más la detección de reuso (Fase 1), no el rate
  limiting.
- **Identidad por `user_id` (del JWT, sin tocar la DB) o IP si no hay token — EXCEPTO en
  `/auth/login` y `/auth/register`, siempre por IP**: protege también los endpoints públicos
  (login, registro, 2FA antes de confirmar) donde todavía no hay usuario. La excepción no es
  cosmética: login/registro no leen el header `Authorization` para autenticar, así que un Bearer
  adjuntado ahí no prueba nada sobre quién hace la request — ver el bug real #2 descrito en el
  Resumen de esta fase (encontrado en la ronda de revisión Agent Teams, no en el diseño inicial).
  **Asunción explícita** sobre el fallback a IP: no hay proxy inverso delante de la API en este
  despliegue, así que `request.client.host` es la IP real de la conexión TCP — el código no lee
  `X-Forwarded-For` (no falsificable hoy). El día que se ponga un proxy/CDN delante (Fase 9/15),
  todas las conexiones se verían con la IP del proxy, fusionando buckets de usuarios distintos;
  fuera de alcance de esta fase, documentado como asunción a revisitar, no como bug.
- **Fail-open si Redis no responde**: la request pasa igual (se loguea el fallo), prioriza
  disponibilidad sobre bloquear toda la API por una caída de infraestructura. No es solo un riesgo
  operacional — ver el escenario de ataque explícito en Riesgos conocidos. Se decidió no
  implementar un fallback de lockout en Postgres independiente de Redis para login/registro:
  duplicaría el propósito de esta fase y añadiría alcance significativo fuera de lo pedido.
- **`BaseHTTPMiddleware`, no ASGI puro**: los problemas conocidos de `BaseHTTPMiddleware` (pérdida
  de `request.state`, manejo de excepciones al encadenar con otros middlewares) aplican sobre todo
  con varios middlewares interactuando — aquí es el único de la app. El patrón
  `dispatch(request, call_next)` es sustancialmente más simple de escribir/testear que ASGI puro
  para esta lógica. Revisar si conviene migrar cuando se añadan más middlewares (ej. CORS si se
  separa el frontend en una fase futura).
- **Cliente Redis síncrono + `run_in_threadpool`, no `redis.asyncio`**: ver el bug real descrito en
  el Resumen de esta fase — un cliente async queda ligado al event loop en el que se usa por
  primera vez, riesgo real de arquitectura, no solo un artefacto de tests.

**Fase 7 — Contenedores**

- **Multi-stage, aunque casi ninguna dependencia necesite compilarse**: verificado explícitamente
  (revisión DevOps) que `psycopg[binary]`, `cryptography`, `bcrypt`, `qrcode[pil]` publican wheels
  manylinux para `python:3.12-slim` — no hace falta gcc/libpq-dev/libffi-dev del sistema. El
  multi-stage aquí no ahorra un toolchain de compilación real (a diferencia del caso de uso típico
  que lo justifica); su valor es separar la etapa de build (pip, caché) de la imagen final, y
  asegurar mecánicamente que `requirements-dev.txt` nunca llega a producción. Aceptado como la
  única pieza del diseño sin beneficio técnico contundente, pero consistente con demostrar
  prácticas de imagen de producción.
- **`ENV PATH="/opt/venv/bin:$PATH"` explícito tras copiar el venv entre stages**: paso
  imprescindible del patrón "copiar venv" que la primera versión de este plan omitió (hallazgo
  bloqueante de la revisión DevOps y del abogado del diablo antes de implementar) — sin él, ni
  `alembic` ni `uvicorn` existen en el `PATH` de la imagen final y el contenedor no arranca.
- **Import de `app.*` resuelto por `WORKDIR /app` + `prepend_sys_path = .` de `alembic.ini`, no por
  un `PYTHONPATH` extra**: la primera versión de este plan atribuía la solución del import a un
  `ENV PYTHONPATH=/app`, que en realidad no era la pieza que arreglaba nada (corregido tras la
  revisión, ver Resumen). Se mantiene de todos modos `PYTHONDONTWRITEBYTECODE=1
  PYTHONUNBUFFERED=1` por higiene (usuario no-root, logs sin buffer), no como fix de import.
- **Usuario no-root (`useradd --system`), sin necesidad de `chown` selectivo en runtime**:
  verificado (revisión de seguridad) que la app no escribe a disco en ningún momento de su
  ejecución normal (solo sirve `app/static` y loguea a stdout; `alembic upgrade head` solo escribe
  a Postgres) — el `chown -R app:app /app` del build es suficiente, no hace falta ningún volumen
  writable adicional.
- **Migraciones automáticas en el `ENTRYPOINT`, no un paso manual**: `docker/entrypoint.sh` corre
  `alembic upgrade head` antes de `exec`utar `uvicorn`. Riesgo aceptado a propósito, no resuelto en
  esta fase: con más de una réplica arrancando a la vez, cada una correría la migración de forma
  concurrente (Alembic no tiene locking distribuido incorporado) — aceptable mientras esta fase no
  levante réplicas reales; revisar si/cuando la Fase 14 (CD) las introduzca, moviendo la migración
  a un job/init-container separado de las réplicas de la API.
- **Secretos nunca horneados en la imagen**: el `Dockerfile` no copia `.env` ni recibe secretos vía
  `ARG`/`ENV` en build time; `docker-compose.yml` los pasa en runtime vía `env_file: .env`
  (secretos reales del desarrollador) combinado con `environment:` explícito solo para
  `DATABASE_URL`/`REDIS_URL`/`COOKIE_SECURE` (que necesitan apuntar a los hostnames internos de
  compose, no a `localhost`). `.dockerignore` excluye `.env` como defensa en profundidad adicional
  (el build context de `docker build .` sí incluye todo el directorio salvo lo excluido ahí, aunque
  el `Dockerfile` luego solo copie `app/`/`alembic.ini` explícitamente).
- **`COOKIE_SECURE: "false"` fijado explícitamente en el servicio `app` de compose, no delegado al
  `.env`**: el default real en `config.py` es `cookie_secure: bool = True` — sin este override, la
  verificación por `curl` del flujo completo (login→2FA→premium) sobre HTTP local habría fallado
  porque el cliente no reenvía la cookie `Secure` del refresh token (hallazgo confirmado por
  DevOps y el abogado del diablo antes de implementar).
- **`security_opt: no-new-privileges:true` + `cap_drop: ALL` solo en el servicio `app`**, no en
  `postgres`/`redis` (que podrían necesitar capacidades propias para su propio entrypoint/initdb):
  hardening barato y de alto valor de señal para un proyecto de portfolio, sin arriesgar romper
  imágenes de terceros ya funcionando. Se descartó explícitamente ir más lejos (`read_only:
  true`+tmpfs, límites de recursos) por ser sobre-ingeniería sin réplicas ni carga real que
  proteger en esta fase.
- **CI: pass-through de secrets (`docker run -e VAR`, sin `=valor` inline)**: el contenedor hereda
  el valor desde el `env:` del step en vez de interpolarlo en la línea de comando — evita que el
  secreto aparezca en `docker inspect`/logs del contenedor. Defensa en profundidad barata sobre
  secrets que ya son CI-only sin dato real detrás.
- **Base `python:3.12.7-slim-bookworm` pinneada a versión y tag exactos, no `3.12-slim` flotante**:
  reproducibilidad del build a cambio de un trade-off real reconocido explícitamente (revisión de
  seguridad): no recibe parches de seguridad del SO/Python automáticamente. Mantenimiento pendiente
  aceptado: rebuild/bump manual periódico de la versión pinneada.
- **La password de Postgres en `docker-compose.yml` (`user:password`) es la misma credencial
  dev-only** que ya vive hardcodeada ahí y en CI desde la Fase 1 — documentado explícitamente para
  que quede claro que no es un descuido de esta fase, no un secreto real.

## Riesgos conocidos

- ~~Desalineación de versión de Python~~ — **verificado en Fase 5, sin problemas reales**: el
  entorno de desarrollo local usa Python 3.13 (única versión instalada en esta máquina); la
  matriz de CI (Fase 5) corre 3.10/3.11/3.12 de verdad en GitHub Actions. Las tres versiones
  pasaron limpio en el primer PR real (migraciones, ruff, black, mypy `--strict`, pytest con
  `--cov-fail-under=85`) — el riesgo que se venía documentando desde la Fase 0 como teórico quedó
  descartado con datos reales, no solo asumido.
- **Dependencias transitivas sin pinnear**: `requirements.txt` fija fastapi/uvicorn/pytest, pero
  no sus dependencias transitivas (pydantic, starlette, etc.). Local (3.13) y CI (3.10-3.12)
  podrían resolver versiones transitivas distintas — es el punto más probable donde el desfase de
  versión de Python muerda de verdad. Revisar en Fase 4/5 si conviene un lockfile
  (`pip-compile`, `uv lock` o similar).
- ~~Sin rate limiting en `/auth/login` ni `/auth/register`~~ — **resuelto en Fase 6**: tier
  `sensitive` del rate limiter (capacidad 5, refill 1/12s) por IP del cliente.
- **`/auth/register` permite enumerar emails registrados** (responde 409 explícito si el email ya
  existe), a diferencia de `/auth/login` que sí es cuidadoso (401 genérico + timing equalizado
  tanto si el email no existe como si la contraseña es incorrecta). Aceptado como limitación
  conocida para este proyecto de portfolio, no corregido en Fase 1.
- **`COOKIE_SECURE=true` por defecto requiere HTTPS**: en desarrollo local por `http://` hay que
  poner `COOKIE_SECURE=false` en `.env` (así está configurado en el `.env` local), o el navegador
  no enviará la cookie del refresh token.
- ~~`PATCH /users/me/premium` solo exige un access token válido~~ — **resuelto en Fase 3**:
  `POST /users/me/premium/activate` ahora exige 2FA confirmado + contraseña + código TOTP.
- **Pérdida o rotación de `TOTP_ENCRYPTION_KEY`**: deja indescifrables todos los secretos TOTP ya
  guardados, forzando a reconfigurar 2FA a todos los usuarios existentes. No hay estrategia de
  rotación de clave en esta fase (aceptado para el alcance de un portfolio).
- ~~El lockout de TOTP es ad-hoc y por-usuario, no un rate limiter general~~ — **resuelto en
  Fase 6**: el rate limiter general (tier `general`, capacidad 60/min por identidad) cubre además
  el caso de un atacante repartiendo intentos entre muchas cuentas distintas a la vez; el lockout
  de Postgres sigue siendo la protección primaria por-cuenta, más estricta.
- **Fail-open del rate limiter como escenario de ataque, no solo riesgo operacional**: para
  `/auth/login` y `/auth/register` (sin ningún otro control además de este rate limiter — a
  diferencia de `/2fa/verify`/`/premium/activate`, que además tienen el lockout de Postgres), un
  atacante que consiga saturar o tumbar Redis elimina de un plumazo la única protección contra
  fuerza bruta de esos dos endpoints, sin que la API deje de responder (justo lo que fail-open
  prioriza). Aceptado explícitamente para el alcance de este proyecto — ver la justificación
  completa en Decisiones técnicas, Fase 6.
- **No hay endpoint para desactivar 2FA una vez confirmado**: limitación conocida, fuera de
  alcance de esta fase (no lo pedía el enunciado). Un usuario que pierde su dispositivo
  autenticador no tiene forma de recuperar acceso a la activación de premium sin intervención
  manual en la base de datos.
- **El hook de `mypy` en pre-commit requiere el venv activado**: al ser `language: system`, usa
  el `mypy` del `PATH` de quien commitea. Si el venv no está activado (otra terminal, GUI de git),
  el hook falla con `Executable 'mypy' not found` en vez de correr una versión distinta en
  silencio — falla ruidoso, no silencioso, pero sigue siendo fricción a documentar (ver README).
- ~~`pytest-cov` sin umbral obligatorio~~ — **resuelto en Fase 5**: `--cov-fail-under=85` en CI
  (esta entrada quedó sin actualizar hasta la revisión de documentación de la Fase 6).
- **Migraciones automáticas del `ENTRYPOINT` no soportan réplicas concurrentes** (Fase 7): si en el
  futuro hay más de una réplica del contenedor de la API arrancando a la vez, cada una correría
  `alembic upgrade head` de forma concurrente — Alembic no tiene locking distribuido incorporado.
  Aceptado mientras esta fase no levante réplicas reales; a resolver si/cuando la Fase 14 (CD) las
  introduzca (mover la migración a un job/init-container separado).
- **La imagen Docker todavía no se publica en ningún registry** (Fase 7): solo se construye
  localmente/en CI como smoke test, nunca se hace `docker push`. Publicar y versionar la imagen es
  explícitamente Fase 14 (CD).
- **Un único contenedor de la API, sin réplicas reales** (Fase 7): el diseño multi-réplica que ya
  asumía el rate limiter con Redis (Fase 6, justificación de "por qué Redis y no memoria local")
  sigue siendo teórico hasta que haya un balanceador delante — sin fase de proxy/CDN todavía
  (Fase 9/15).
- **Base `python:3.12.7-slim-bookworm` pinneada, sin parches de seguridad automáticos** (Fase 7):
  trade-off aceptado por reproducibilidad del build; requiere un bump manual periódico de la
  versión pinneada, no ocurre solo.

## Cómo escalaría esto en producción real

_Pendiente — sección dedicada en la Fase 15 (CDN, réplicas geográficas, Cassandra para estado de
reproducción, etc.), con notas parciales añadidas en las fases que las motivan (9, 12, 13)._
