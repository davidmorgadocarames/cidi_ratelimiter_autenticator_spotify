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
| 2    | UI de login y toggle de premium           | ⬜ Pendiente    |
| 3    | 2FA (TOTP)                                | ⬜ Pendiente    |
| 4    | Calidad de código local                   | ⬜ Pendiente    |
| 5    | CI                                        | ⬜ Pendiente    |
| 6    | Rate limiter con Redis                    | ⬜ Pendiente    |
| 7    | Contenedores                              | ⬜ Pendiente    |
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
  `docker-compose.yml`, el resto del stack completo (API dockerizada, healthchecks cruzados) sigue
  siendo Fase 7.
- **Mitigación de timing side-channel en `/auth/login`**: cuando el email no existe, se ejecuta
  igualmente un hash bcrypt contra un valor "dummy" para que la respuesta tarde lo mismo que con
  una contraseña incorrecta, evitando enumerar emails registrados por diferencia de tiempo.
- **`JWT_SECRET_KEY` no puede quedar en su valor placeholder**: `Settings` rechaza arrancar si
  sigue en `"change-me"` (falla rápido en vez de firmar tokens con un secreto público conocido).

## Riesgos conocidos

- **Desalineación de versión de Python**: el entorno de desarrollo local usa Python 3.13 (única
  versión instalada en esta máquina), mientras que la matriz de CI planificada para la Fase 5 es
  3.10/3.11/3.12. Es posible que aparezcan diferencias de comportamiento entre local y CI; se
  revisará si surge algún problema concreto de compatibilidad.
- **Dependencias transitivas sin pinnear**: `requirements.txt` fija fastapi/uvicorn/pytest, pero
  no sus dependencias transitivas (pydantic, starlette, etc.). Local (3.13) y CI (3.10-3.12)
  podrían resolver versiones transitivas distintas — es el punto más probable donde el desfase de
  versión de Python muerda de verdad. Revisar en Fase 4/5 si conviene un lockfile
  (`pip-compile`, `uv lock` o similar).
- **Sin rate limiting en `/auth/login` ni `/auth/register`**: pospuesto explícitamente a la Fase 6
  (rate limiter con Redis). Hasta entonces, ambos endpoints son vulnerables a fuerza bruta /
  credential stuffing sin fricción.
- **`/auth/register` permite enumerar emails registrados** (responde 409 explícito si el email ya
  existe), a diferencia de `/auth/login` que sí es cuidadoso (401 genérico + timing equalizado
  tanto si el email no existe como si la contraseña es incorrecta). Aceptado como limitación
  conocida para este proyecto de portfolio, no corregido en Fase 1.
- **`COOKIE_SECURE=true` por defecto requiere HTTPS**: en desarrollo local por `http://` hay que
  poner `COOKIE_SECURE=false` en `.env` (así está configurado en el `.env` local), o el navegador
  no enviará la cookie del refresh token.

## Cómo escalaría esto en producción real

_Pendiente — sección dedicada en la Fase 15 (CDN, réplicas geográficas, Cassandra para estado de
reproducción, etc.), con notas parciales añadidas en las fases que las motivan (9, 12, 13)._
