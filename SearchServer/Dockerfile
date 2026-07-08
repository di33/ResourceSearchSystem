# syntax=docker/dockerfile:1.7

FROM python:3.12-slim AS base

WORKDIR /app
ENV PYTHONPATH=/app:/app/Scripts

ARG DEBIAN_MIRROR=https://deb.debian.org/debian
ARG DEBIAN_SECURITY_MIRROR=https://deb.debian.org/debian-security

RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    set -eux; \
    rm -f /etc/apt/apt.conf.d/docker-clean; \
    find /etc/apt -type f \( -name '*.list' -o -name '*.sources' \) -exec sed -i \
        -e "s|http://deb.debian.org/debian|${DEBIAN_MIRROR}|g" \
        -e "s|http://deb.debian.org/debian-security|${DEBIAN_SECURITY_MIRROR}|g" \
        {} +; \
    apt_opts="-o Acquire::Retries=8 -o Acquire::http::Timeout=30 -o Acquire::https::Timeout=30 -o Acquire::http::Pipeline-Depth=0 -o Acquire::https::Pipeline-Depth=0 -o Acquire::http::No-Cache=true"; \
    apt_retry() { \
        tries=0; \
        until apt-get $apt_opts "$@"; do \
            tries=$((tries + 1)); \
            if [ "$tries" -ge 5 ]; then \
                return 1; \
            fi; \
            sleep $((tries * 10)); \
        done; \
    }; \
    apt_retry update; \
    apt_retry install -y --no-install-recommends curl; \
    rm -rf /var/lib/apt/lists/*

FROM base AS deps

COPY SearchServer/requirements.txt .
RUN --mount=type=cache,target=/root/.cache/pip,sharing=locked \
    pip install -r requirements.txt

FROM deps AS app

COPY SearchServer .
COPY resource_contracts /app/resource_contracts

# ---------- development ----------
FROM app AS dev
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--reload", "--reload-dir", "/app"]

# ---------- production dependencies ----------
FROM deps AS prod-deps
RUN --mount=type=cache,target=/root/.cache/pip,sharing=locked \
    pip install gunicorn

# ---------- production ----------
FROM prod-deps AS prod
COPY SearchServer .
COPY resource_contracts /app/resource_contracts
CMD ["gunicorn", "app.main:app", \
     "-k", "uvicorn.workers.UvicornWorker", \
     "--bind", "0.0.0.0:8000", \
     "--workers", "4", \
     "--max-requests", "2000", \
     "--max-requests-jitter", "200", \
     "--timeout", "120", \
     "--graceful-timeout", "30", \
     "--access-logfile", "-"]
