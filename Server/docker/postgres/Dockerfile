FROM postgres:16-bookworm

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    cmake \
    git \
    ca-certificates \
    postgresql-server-dev-16 \
    && rm -rf /var/lib/apt/lists/*

RUN git clone https://github.com/jaiminpan/pg_jieba.git /tmp/pg_jieba \
    && cd /tmp/pg_jieba \
    && git submodule update --init --recursive \
    && mkdir build && cd build \
    && cmake -DCMAKE_BUILD_TYPE=Release \
         -DPostgreSQL_TYPE_INCLUDE_DIR=/usr/include/postgresql/16/server \
         .. \
    && make \
    && make install \
    && cd / && rm -rf /tmp/pg_jieba

RUN apt-get purge -y build-essential cmake git \
    && apt-get autoremove -y \
    && rm -rf /var/lib/apt/lists/*
