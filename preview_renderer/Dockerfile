FROM python:3.12-slim AS base

ARG INSTALL_BLENDER=false
ARG RP_APT_VENDOR_MODE=auto
ARG RP_EXTRA_APT_PACKAGES=""

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app:/app/Tools \
    PR_SHARED_RESOURCE_PROCESSOR_PATH=/app/Tools \
    PR_WORK_DIR=/app/data/preview_renderer \
    PR_KEEP_WORK_DIR=false

COPY resource_processing_server/docker/apt-packages*.txt /tmp/resource-processing-apt/
COPY resource_processing_server/docker/vendor/apt/ /opt/resource-processing-vendor/apt/
RUN set -eux; \
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
    packages="$(sed -e 's/#.*//' -e '/^[[:space:]]*$/d' /tmp/resource-processing-apt/apt-packages.txt | tr '\n' ' ')"; \
    if [ "$INSTALL_BLENDER" = "true" ]; then \
        packages="$packages $(sed -e 's/#.*//' -e '/^[[:space:]]*$/d' /tmp/resource-processing-apt/apt-packages.blender.txt | tr '\n' ' ')"; \
    fi; \
    packages="$packages $RP_EXTRA_APT_PACKAGES"; \
    vendor_dir=/opt/resource-processing-vendor/apt; \
    if [ -f "$vendor_dir/manifest.json" ] && [ -f "$vendor_dir/Packages" ]; then \
        printf 'deb [trusted=yes] file:%s ./\n' "$vendor_dir" > /tmp/resource-processing-vendor.list; \
        vendor_source_opts="-o Dir::Etc::sourcelist=/tmp/resource-processing-vendor.list -o Dir::Etc::sourceparts=- -o APT::Get::List-Cleanup=0"; \
        apt_retry $vendor_source_opts update; \
        apt_retry $vendor_source_opts install -y --no-install-recommends $packages; \
    elif [ "$RP_APT_VENDOR_MODE" = "required" ]; then \
        echo "RP_APT_VENDOR_MODE=required but resource_processing_server/docker/vendor/apt has no complete local apt repo" >&2; \
        exit 1; \
    else \
        apt_retry update; \
        apt_retry install -y --no-install-recommends $packages; \
    fi; \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

FROM base AS deps

COPY Tools/requirements.txt /app/Tools/requirements.txt
COPY preview_renderer/requirements.txt /app/preview_renderer/requirements.txt
COPY resource_processing_server/docker/vendor/pip/ /opt/resource-processing-vendor/pip/
RUN set -eux; \
    pip_vendor=/opt/resource-processing-vendor/pip; \
    if [ -f "$pip_vendor/manifest.json" ] && find "$pip_vendor" -type f \( -name '*.whl' -o -name '*.tar.gz' -o -name '*.zip' \) | grep -q .; then \
        python -m pip install --no-cache-dir --no-index --find-links="$pip_vendor" -r /app/preview_renderer/requirements.txt; \
    else \
        python -m pip install --no-cache-dir --upgrade pip; \
        python -m pip install --no-cache-dir -r /app/preview_renderer/requirements.txt; \
    fi

COPY Tools/spine_preview/package*.json /app/Tools/spine_preview/
COPY resource_processing_server/docker/vendor/npm/ /opt/resource-processing-vendor/npm/
RUN cd /app/Tools/spine_preview \
    && if [ -f /opt/resource-processing-vendor/npm/manifest.json ] && [ -d /opt/resource-processing-vendor/npm/_cacache ]; then \
        npm ci --offline --omit=dev --no-audit --no-fund --cache /opt/resource-processing-vendor/npm; \
    else \
        npm ci --omit=dev --no-audit --no-fund; \
    fi

FROM deps AS app

COPY resource_contracts /app/resource_contracts
COPY Tools /app/Tools
COPY preview_renderer /app/preview_renderer

RUN mkdir -p /app/data/preview_renderer

EXPOSE 8200

CMD ["python", "-m", "uvicorn", "preview_renderer.app.main:app", "--host", "0.0.0.0", "--port", "8200"]
