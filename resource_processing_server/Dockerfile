ARG PROCESSING_BASE_IMAGE=resource-upload/processing-base:local
FROM ${PROCESSING_BASE_IMAGE} AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app:/app/Tools \
    RP_SHARED_RESOURCE_PROCESSOR_PATH=/app/Tools \
    RP_WORK_DIR=/app/data/resource_processing_server \
    RP_SNAPSHOT_DB_PATH=/app/data/resource_processing_server/snapshots.db

WORKDIR /app

COPY resource_contracts /app/resource_contracts
COPY Tools /app/Tools
COPY resource_processing_server /app/resource_processing_server

RUN mkdir -p /app/data/resource_processing_server

EXPOSE 8100

CMD ["python", "-m", "uvicorn", "resource_processing_server.app.main:app", "--host", "0.0.0.0", "--port", "8100"]
