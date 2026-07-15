# syntax=docker/dockerfile:1.7

FROM python:3.12-slim

ARG INSTALL_BLENDER=false
ARG RP_VENDOR_MODE=auto
ARG RP_EXTRA_APT_PACKAGES=""

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app:/app/Tools

COPY resource_processing_server/docker/apt-packages*.txt /tmp/resource-processing-apt/
COPY resource_processing_server/docker/vendor/apt/ /opt/resource-processing-vendor/apt/
RUN set -eux; \
    case "$RP_VENDOR_MODE" in auto|required|online) ;; *) echo "RP_VENDOR_MODE must be auto, required, or online" >&2; exit 2;; esac; \
    apt_opts="-o Acquire::Retries=8 -o Acquire::http::Timeout=30 -o Acquire::https::Timeout=30"; \
    packages="$(sed -e 's/#.*//' -e '/^[[:space:]]*$/d' /tmp/resource-processing-apt/apt-packages.txt | tr '\n' ' ')"; \
    if [ "$INSTALL_BLENDER" = "true" ]; then \
        packages="$packages $(sed -e 's/#.*//' -e '/^[[:space:]]*$/d' /tmp/resource-processing-apt/apt-packages.blender.txt | tr '\n' ' ')"; \
    fi; \
    packages="$packages $RP_EXTRA_APT_PACKAGES"; \
    vendor_dir=/opt/resource-processing-vendor/apt; \
    if [ "$RP_VENDOR_MODE" != "online" ] \
       && [ -f "$vendor_dir/manifest.json" ] \
       && [ -f "$vendor_dir/Packages" ] \
       && find "$vendor_dir" -type f -name '*.deb' | grep -q .; then \
        echo "APT dependency source: vendor"; \
        printf 'deb [trusted=yes] file:%s ./\n' "$vendor_dir" > /tmp/resource-processing-vendor.list; \
        apt-get $apt_opts -o Dir::Etc::sourcelist=/tmp/resource-processing-vendor.list -o Dir::Etc::sourceparts=- update; \
        apt-get $apt_opts -o Dir::Etc::sourcelist=/tmp/resource-processing-vendor.list -o Dir::Etc::sourceparts=- install -y --no-install-recommends $packages; \
    elif [ "$RP_VENDOR_MODE" = "required" ]; then \
        echo "RP_VENDOR_MODE=required but the APT vendor payload is incomplete" >&2; exit 1; \
    else \
        echo "APT dependency source: online"; \
        apt-get $apt_opts update; apt-get $apt_opts install -y --no-install-recommends $packages; \
    fi; \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY Tools/requirements.txt /app/Tools/requirements.txt
COPY preview_renderer/requirements.txt /app/preview_renderer/requirements.txt
COPY resource_processing_server/requirements.txt /app/resource_processing_server/requirements.txt
COPY resource_processing_server/docker/vendor/pip/ /opt/resource-processing-vendor/pip/
RUN set -eux; \
    pip_vendor=/opt/resource-processing-vendor/pip; \
    if [ "$RP_VENDOR_MODE" != "online" ] \
       && find "$pip_vendor" -type f \( -name '*.whl' -o -name '*.tar.gz' -o -name '*.zip' \) | grep -q .; then \
        echo "pip dependency source: vendor"; \
        python -m pip install --no-cache-dir --no-index --find-links="$pip_vendor" \
          -r /app/preview_renderer/requirements.txt \
          -r /app/resource_processing_server/requirements.txt; \
    elif [ "$RP_VENDOR_MODE" = "required" ]; then \
        echo "RP_VENDOR_MODE=required but the pip vendor payload is incomplete" >&2; exit 1; \
    else \
        echo "pip dependency source: online"; \
        python -m pip install --no-cache-dir --upgrade pip; \
        python -m pip install --no-cache-dir \
          -r /app/preview_renderer/requirements.txt \
          -r /app/resource_processing_server/requirements.txt; \
    fi

COPY Tools/spine_preview/package*.json /app/Tools/spine_preview/
COPY resource_processing_server/docker/vendor/npm/ /opt/resource-processing-vendor/npm/
RUN set -eux; \
    cd /app/Tools/spine_preview; \
    npm_vendor=/opt/resource-processing-vendor/npm; \
    if [ "$RP_VENDOR_MODE" != "online" ] \
       && [ -d "$npm_vendor/_cacache" ] \
       && find "$npm_vendor/_cacache" -type f | grep -q .; then \
        echo "npm dependency source: vendor"; \
        npm ci --offline --omit=dev --no-audit --no-fund --cache "$npm_vendor"; \
    elif [ "$RP_VENDOR_MODE" = "required" ]; then \
        echo "RP_VENDOR_MODE=required but the npm vendor payload is incomplete" >&2; exit 1; \
    else \
        echo "npm dependency source: online"; \
        npm ci --omit=dev --no-audit --no-fund; \
    fi
