from unittest.mock import Mock

import pytest

from app.services import embedding_service


def _make_complete_model_directory(path):
    path.mkdir(parents=True)
    (path / "modules.json").write_text("[]", encoding="utf-8")
    (path / "pytorch_model.bin").write_bytes(b"test weights")
    return path


def _mock_model_loader(monkeypatch):
    model = object()
    loader = Mock(return_value=model)
    monkeypatch.setattr(embedding_service, "SentenceTransformer", loader)
    monkeypatch.setattr(embedding_service, "_model", None)
    return model, loader


def test_loads_model_from_hugging_face_cache_snapshot(tmp_path, monkeypatch):
    model_dir = tmp_path / "models"
    snapshot = _make_complete_model_directory(
        model_dir
        / "models--BAAI--bge-m3"
        / "snapshots"
        / "test-revision"
    )
    ref_path = model_dir / "models--BAAI--bge-m3" / "refs" / "main"
    ref_path.parent.mkdir(parents=True)
    ref_path.write_text("test-revision", encoding="utf-8")
    model, loader = _mock_model_loader(monkeypatch)
    monkeypatch.setattr(embedding_service, "MODEL_DIR", model_dir)

    loaded_model = embedding_service.get_embedding_model()

    assert loaded_model is model
    assert loader.call_args.args[0] == str(snapshot)
    assert loader.call_args.kwargs["local_files_only"] is True


def test_missing_model_fails_without_remote_fallback(tmp_path, monkeypatch):
    model_dir = tmp_path / "missing-models"
    _, loader = _mock_model_loader(monkeypatch)
    monkeypatch.setattr(embedding_service, "MODEL_DIR", model_dir)

    with pytest.raises(RuntimeError, match="未找到完整的本地 BGE-M3 模型"):
        embedding_service.get_embedding_model()

    assert not model_dir.exists()
    loader.assert_not_called()


def test_incomplete_cached_snapshot_fails_without_loading(
    tmp_path,
    monkeypatch,
):
    model_dir = tmp_path / "models"
    snapshot = model_dir / "models--BAAI--bge-m3" / "snapshots" / "partial"
    snapshot.mkdir(parents=True)
    (snapshot / "modules.json").write_text("[]", encoding="utf-8")
    ref_path = model_dir / "models--BAAI--bge-m3" / "refs" / "main"
    ref_path.parent.mkdir(parents=True)
    ref_path.write_text("partial", encoding="utf-8")
    _, loader = _mock_model_loader(monkeypatch)
    monkeypatch.setattr(embedding_service, "MODEL_DIR", model_dir)

    with pytest.raises(RuntimeError, match="未找到完整的本地 BGE-M3 模型"):
        embedding_service.get_embedding_model()

    loader.assert_not_called()


def test_loads_legacy_local_model_directory(tmp_path, monkeypatch):
    model_dir = tmp_path / "models"
    legacy_path = _make_complete_model_directory(
        model_dir / "BAAI" / "bge-m3"
    )
    model, loader = _mock_model_loader(monkeypatch)
    monkeypatch.setattr(embedding_service, "MODEL_DIR", model_dir)

    loaded_model = embedding_service.get_embedding_model()

    assert loaded_model is model
    assert loader.call_args.args[0] == str(legacy_path)
    assert loader.call_args.kwargs["local_files_only"] is True
