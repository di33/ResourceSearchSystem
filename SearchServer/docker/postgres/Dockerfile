# syntax=docker/dockerfile:1.7

FROM python:3.12-slim AS certs

FROM postgres:16-bookworm AS base

ARG DEBIAN_MIRROR=https://deb.debian.org/debian
ARG DEBIAN_SECURITY_MIRROR=https://deb.debian.org/debian-security
ARG POSTGRES_APT_MIRROR=https://apt.postgresql.org/pub/repos/apt

COPY --from=certs /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/ca-certificates.crt

RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    set -eux; \
    rm -f /etc/apt/apt.conf.d/docker-clean; \
    find /etc/apt -type f \( -name '*.list' -o -name '*.sources' \) -exec sed -i \
        -e "s|http://deb.debian.org/debian|${DEBIAN_MIRROR}|g" \
        -e "s|http://deb.debian.org/debian-security|${DEBIAN_SECURITY_MIRROR}|g" \
        -e "s|http://apt.postgresql.org/pub/repos/apt|${POSTGRES_APT_MIRROR}|g" \
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
    apt_retry install -y --no-install-recommends \
        build-essential \
        cmake \
        ca-certificates \
        postgresql-server-dev-16 \
    && rm -rf /var/lib/apt/lists/*

FROM base AS deps

COPY vendor/pg_jieba /tmp/pg_jieba

RUN cd /tmp/pg_jieba \
    && mkdir build && cd build \
    && cmake -DCMAKE_BUILD_TYPE=Release \
         -DPostgreSQL_TYPE_INCLUDE_DIR=/usr/include/postgresql/16/server \
         .. \
    && make \
    && make install \
    && cd / && rm -rf /tmp/pg_jieba

FROM deps AS app

RUN apt-get purge -y build-essential cmake postgresql-server-dev-16 \
    && apt-get autoremove -y \
    && rm -rf /var/lib/apt/lists/*
