from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=(PROJECT_ROOT / ".env", PROJECT_ROOT / "backend" / ".env"), extra="ignore")

    app_env: str = "development"
    database_path: Path = PROJECT_ROOT / "data" / "incidentpilot.sqlite3"
    llm_provider: Literal["demo", "gemini", "openai-compatible"] = "demo"
    gemini_api_key: SecretStr | None = None
    gemini_model: str = "gemini-3.5-flash-lite"
    poc_username: str | None = None
    poc_password: SecretStr | None = None
    require_persistent_volume: bool = False
    openai_api_key: str | None = None
    openai_base_url: str = "https://api.openai.com/v1"
    openai_model: str = "gpt-4.1-mini"
    langsmith_tracing: bool = False
    langsmith_api_key: str | None = None
    langsmith_project: str = "incidentpilot-demo"
    frontend_origin: str = "http://localhost:5173"


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    settings.database_path.parent.mkdir(parents=True, exist_ok=True)
    return settings
