from types import SimpleNamespace
from unittest.mock import Mock

from app.config import BACKEND_ROOT, Settings, get_settings
from app.services import embedding_service


def test_chroma_defaults_to_backend_data_directory(monkeypatch):
    monkeypatch.setenv("CHROMA_DIR", "")
    get_settings.cache_clear()
    try:
        settings = Settings()
        expected = (BACKEND_ROOT / "data" / "chroma_db").resolve()
        assert settings.chroma_path == expected
        assert get_settings().chroma_path == expected
    finally:
        get_settings.cache_clear()


def test_chroma_dir_environment_override(tmp_path, monkeypatch):
    custom_dir = tmp_path / "isolated-chroma"
    monkeypatch.setenv("CHROMA_DIR", str(custom_dir))
    get_settings.cache_clear()
    try:
        settings = get_settings()
        assert settings.chroma_dir == str(custom_dir)
        assert settings.chroma_path == custom_dir.resolve()
        assert embedding_service.get_chroma_dir() == custom_dir.resolve()
    finally:
        get_settings.cache_clear()


def test_chroma_client_and_stats_use_configured_path(tmp_path, monkeypatch):
    configured_dir = (tmp_path / "configured-chroma").resolve()
    collection = Mock()
    collection.count.return_value = 0
    client = Mock()
    client.get_or_create_collection.return_value = collection
    persistent_client = Mock(return_value=client)

    monkeypatch.setattr(
        embedding_service,
        "get_settings",
        lambda: SimpleNamespace(chroma_path=configured_dir),
    )
    monkeypatch.setattr(embedding_service, "_collection", None)
    monkeypatch.setattr(embedding_service, "_chroma_client", None)
    monkeypatch.setattr(
        embedding_service.chromadb,
        "PersistentClient",
        persistent_client,
    )

    loaded_collection = embedding_service.get_chroma_collection()
    stats = embedding_service.get_vector_index_stats()

    assert loaded_collection is collection
    persistent_client.assert_called_once_with(path=str(configured_dir))
    assert configured_dir.is_dir()
    assert stats["storage_path"] == str(configured_dir)
