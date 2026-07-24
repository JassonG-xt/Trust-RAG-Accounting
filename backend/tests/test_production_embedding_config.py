# backend/tests/test_production_embedding_config.py
from backend.app.core.config import Settings


def test_production_env_flips_bge_m3_defaults(monkeypatch):
    for var in ("EMBEDDING_PROVIDER", "EMBEDDING_MODEL", "EMBEDDING_DIMENSION",
                "RETRIEVAL_FUSION_MODE", "RERANKER_PROVIDER"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("APP_ENV", "production")
    settings = Settings()
    assert settings.embedding_provider == "sentence_transformers"
    assert settings.embedding_model == "BAAI/bge-m3"
    assert settings.embedding_dimension == 1024
    assert settings.retrieval_fusion_mode == "rrf"
    assert settings.reranker_provider == "bge"
