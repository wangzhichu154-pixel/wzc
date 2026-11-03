# syntax=docker/dockerfile:1.7
FROM debian:trixie-slim AS engine-build
RUN --mount=type=secret,id=proxy_ca,mode=0444 \
    set --; if [ -f /run/secrets/proxy_ca ]; then set -- -o Acquire::https::CaInfo=/run/secrets/proxy_ca; fi; \
    apt-get "$@" update && apt-get "$@" install -y --no-install-recommends g++ cmake make python3 ca-certificates && rm -rf /var/lib/apt/lists/*
WORKDIR /src
COPY engine/ ./engine/
RUN cmake -S engine -B /build -DCMAKE_BUILD_TYPE=Release && cmake --build /build -j2 && ctest --test-dir /build --output-on-failure

FROM python:3.12-slim-trixie AS python-build
WORKDIR /build
COPY requirements.txt .
RUN --mount=type=secret,id=proxy_ca,mode=0444 \
    if [ -f /run/secrets/proxy_ca ]; then export PIP_CERT=/run/secrets/proxy_ca; fi; \
    pip wheel --only-binary=:all: --no-cache-dir --wheel-dir /wheels -r requirements.txt

FROM python:3.12-slim-trixie AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app \
    ENGINE_BINARY=/app/build/trading-replay \
    VECTOR_CORE_LIBRARY=/app/build/libvector_core.so \
    STATIC_ROOT=/app/staticfiles MEDIA_ROOT=/app/media EMAIL_FILE_PATH=/app/emails
RUN --mount=type=secret,id=proxy_ca,mode=0444 \
    set --; if [ -f /run/secrets/proxy_ca ]; then set -- -o Acquire::https::CaInfo=/run/secrets/proxy_ca; fi; \
    apt-get "$@" update && apt-get "$@" upgrade -y --no-install-recommends \
    && apt-get "$@" install -y --no-install-recommends libstdc++6 ca-certificates && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 app && useradd --uid 10001 --gid app --no-create-home --shell /usr/sbin/nologin app
COPY --from=python-build /wheels /wheels
COPY requirements.txt /app/requirements.txt
COPY --chmod=0444 VERSION /app/VERSION
RUN pip install --no-cache-dir --no-index --find-links=/wheels -r /app/requirements.txt && rm -rf /wheels
COPY --from=engine-build /build/trading-replay /app/build/trading-replay
COPY --from=engine-build /build/trading-ml-replay /app/build/trading-ml-replay
COPY --from=engine-build /build/libvector_core.so /app/build/libvector_core.so
COPY --chown=app:app backend/ /app/backend/
COPY --chown=app:app execution/ /app/execution/
COPY --chown=app:app ml/ /app/ml/
COPY --chown=app:app infrastructure/docker/app-entrypoint.sh /app/entrypoint.sh
RUN mkdir -p /app/staticfiles /app/media /app/emails && chown app:app /app/staticfiles /app/media /app/emails && chmod 0700 /app/emails && chmod 0755 /app/entrypoint.sh
WORKDIR /app/backend
USER 10001:10001
RUN python manage.py makemigrations --check --dry-run --settings=config.testing
EXPOSE 8000
ENTRYPOINT ["/app/entrypoint.sh"]
CMD ["gunicorn", "config.wsgi:application", "-c", "config/gunicorn.py", "--bind", "0.0.0.0:8000", "--workers", "2", "--timeout", "60", "--error-logfile", "-"]
