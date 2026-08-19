# Arquitectura

> Este documento se construye de forma incremental, fase a fase. Cada sección se completa cuando
> la fase correspondiente del roadmap (ver `README.md`) se implementa.

## Resumen

_Pendiente — se completa a partir de la Fase 1, cuando exista una API real que describir._

## Diagrama de arquitectura

_Pendiente — diagrama completo (Mermaid o imagen) planificado para la Fase 15._

## Fases implementadas vs diseño teórico

Esta tabla distingue qué partes del proyecto están realmente implementadas y funcionando, y
cuáles quedan como diseño documentado (explicado pero no construido, por alcance/tiempo de un
proyecto de portfolio).

| Fase | Descripción                              | Estado        |
| ---- | ----------------------------------------- | ------------- |
| 0    | Scaffolding del proyecto                  | ✅ Implementado |
| 1    | API base y autenticación (JWT)            | ⬜ Pendiente    |
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

_Pendiente — se documentará cada decisión (por qué Redis, por qué token bucket, por qué TOTP, por
qué Postgres, etc.) en la fase en que se toma._

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

## Cómo escalaría esto en producción real

_Pendiente — sección dedicada en la Fase 15 (CDN, réplicas geográficas, Cassandra para estado de
reproducción, etc.), con notas parciales añadidas en las fases que las motivan (9, 12, 13)._
