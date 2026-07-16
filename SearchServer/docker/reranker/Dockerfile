ARG RERANKER_BASE_IMAGE=resource-upload/reranker-base:local
FROM ${RERANKER_BASE_IMAGE}

ARG DOWNLOAD_MODEL=false
ARG RERANKER_MODEL=BAAI/bge-reranker-v2-m3
COPY download_model.py .
RUN if [ "$DOWNLOAD_MODEL" = "true" ]; then \
        RERANKER_MODEL="${RERANKER_MODEL}" python download_model.py; \
    fi

COPY app.py download_model.py model_paths.py .

EXPOSE 8100

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8100"]
