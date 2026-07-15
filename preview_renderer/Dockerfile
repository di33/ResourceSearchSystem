ARG PROCESSING_BASE_IMAGE=resource-upload/processing-base:local
FROM ${PROCESSING_BASE_IMAGE} AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app:/app/Tools \
    PR_SHARED_RESOURCE_PROCESSOR_PATH=/app/Tools \
    PR_WORK_DIR=/app/data/preview_renderer \
    PR_KEEP_WORK_DIR=false

WORKDIR /app

COPY resource_contracts /app/resource_contracts
COPY Tools /app/Tools
COPY preview_renderer /app/preview_renderer

RUN mkdir -p /app/data/preview_renderer

EXPOSE 8200

CMD ["python", "-m", "uvicorn", "preview_renderer.app.main:app", "--host", "0.0.0.0", "--port", "8200"]
