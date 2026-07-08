# syntax=docker/dockerfile:1.7

FROM python:3.12-slim AS base

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

WORKDIR /app

FROM base AS deps

COPY requirements.txt .
RUN --mount=type=cache,target=/root/.cache/pip,sharing=locked \
    pip install -r requirements.txt

ARG DOWNLOAD_MODEL=false
ARG RERANKER_MODEL=BAAI/bge-reranker-v2-m3
COPY download_model.py .
RUN if [ "$DOWNLOAD_MODEL" = "true" ]; then \
        RERANKER_MODEL="${RERANKER_MODEL}" python download_model.py; \
    fi

FROM deps AS app

COPY app.py download_model.py .

EXPOSE 8100

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8100"]
