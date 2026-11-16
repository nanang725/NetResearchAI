from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

BACKEND_ROOT = Path(__file__).resolve().parent.parent
PROJECT_ROOT = BACKEND_ROOT.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(PROJECT_ROOT / ".env", BACKEND_ROOT / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = "NetResearch AI"
    app_env: str = "development"
    api_host: str = "0.0.0.0"
    api_port: int = 8000
    database_url: str = f"sqlite:///{(BACKEND_ROOT / 'data' / 'netresearch.db').as_posix()}"
    upload_dir: str = str(BACKEND_ROOT / "data" / "uploads")
    chroma_dir: str = str(BACKEND_ROOT / "data" / "chroma_db")
    max_upload_mb: int = 50
    max_batch_files: int = Field(default=20, ge=1, le=100)
    max_batch_upload_mb: int = Field(default=200, ge=1)
    ocr_enabled: bool = True
    ocr_language: str = "eng+chi_sim"
    ocr_dpi: int = Field(default=200, ge=72, le=600)
    ocr_min_text_chars: int = Field(default=30, ge=0)
    semantic_duplicate_threshold: float = Field(default=0.90, ge=0, le=1)
    cors_origins: str = "http://localhost:5173,http://127.0.0.1:5173"

    # ================= 新增：大模型配置 =================
    llm_api_key: str = ""
    llm_base_url: str = "https://api.deepseek.com/v1"
    llm_model_name: str = "deepseek-chat"
    # ====================================================

    @property
    def cors_origin_list(self) -> list[str]:
        return [item.strip() for item in self.cors_origins.split(",") if item.strip()]

    @property
    def upload_path(self) -> Path:
        return Path(self.upload_dir).resolve()

    @property
    def chroma_path(self) -> Path:
        configured = self.chroma_dir.strip()
        path = (
            Path(configured)
            if configured
            else BACKEND_ROOT / "data" / "chroma_db"
        )
        return path.expanduser().resolve()

    @property
    def max_upload_bytes(self) -> int:
        return self.max_upload_mb * 1024 * 1024


@lru_cache
def get_settings() -> Settings:
    return Settings()
