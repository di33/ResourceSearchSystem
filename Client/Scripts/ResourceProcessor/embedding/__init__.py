"""Embedding provider registration — auto-registers all providers on import."""

# Import provider modules so they self-register with EmbeddingFactory
try:
    import ResourceProcessor.embedding.dashscope_embedding_provider  # noqa: F401
except Exception:
    pass

try:
    import ResourceProcessor.embedding.zhipu_embedding_provider  # noqa: F401
except Exception:
    pass

try:
    import ResourceProcessor.embedding.ksyun_embedding_provider  # noqa: F401
except Exception:
    pass
