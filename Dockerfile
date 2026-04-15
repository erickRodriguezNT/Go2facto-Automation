# ─── Stage 1: builder ─────────────────────────────────────────────────────────
FROM python:3.13.1-alpine AS builder

# Dependencias de compilación para extensiones C (Werkzeug, etc.)
RUN apk add --no-cache \
    gcc \
    musl-dev \
    libffi-dev

WORKDIR /app

COPY requirements.txt .

# Instalar en prefijo aislado para copiar limpio al stage final
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir --prefix=/install -r requirements.txt

# ─── Stage 2: production ──────────────────────────────────────────────────────
FROM python:3.13.1-alpine AS production

# Alpine usa addgroup/adduser (BusyBox), no groupadd/useradd
RUN addgroup -g 1001 appuser \
    && adduser -u 1001 -G appuser -s /bin/sh -D appuser

WORKDIR /app

# Solo los paquetes instalados — sin gcc, musl-dev ni cache de pip
COPY --from=builder /install /usr/local

# Código fuente con ownership correcto
COPY --chown=appuser:appuser . .

USER appuser

EXPOSE 8080

# Alpine incluye wget nativo (no curl)
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD wget -qO- http://localhost:8080/health || exit 1

CMD ["gunicorn", "--bind", "0.0.0.0:8080", "--workers", "4", "--timeout", "120", "app:app"]
