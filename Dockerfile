# Etapa 1: build - instala solo dependencias de runtime (requirements.txt, nunca
# requirements-dev.txt) en un virtualenv aislado. La mayoria de estas deps (psycopg[binary],
# cryptography, bcrypt, qrcode[pil]) publican wheels manylinux para esta imagen, asi que no
# hace falta compilador/headers de sistema - el multi-stage aqui separa la etapa de build de
# la de runtime por higiene de imagen, no porque haya nada que compilar de verdad.
FROM python:3.12.7-slim-bookworm AS builder

WORKDIR /build

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Etapa 2: runtime - solo el venv ya construido + el codigo de la app, sin pip/cache/build tools.
FROM python:3.12.7-slim-bookworm

RUN groupadd --system app && useradd --system --gid app --no-create-home app

# ffmpeg no es un paquete pip - se necesita el binario de sistema (junto con
# ffprobe, incluido en el mismo paquete Debian) para transcodificar audio
# (Fase 8). Crece la imagen de forma no trivial (~150-200MB, cuantificado en
# docs/architecture.md) - trade-off aceptado, es funcionalidad requerida.
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY --from=builder /opt/venv /opt/venv
# Imprescindible: sin esto ni alembic ni uvicorn existen en el PATH de esta etapa
# (la imagen slim solo trae el Python base) y el ENTRYPOINT muere en la primera linea.
ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

COPY app/ ./app
COPY alembic.ini ./
COPY docker/entrypoint.sh ./docker/entrypoint.sh

RUN chmod +x ./docker/entrypoint.sh && chown -R app:app /app

USER app

EXPOSE 8000

HEALTHCHECK --interval=10s --timeout=3s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8000/health').status == 200 else 1)"

ENTRYPOINT ["./docker/entrypoint.sh"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
