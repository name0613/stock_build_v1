from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_name: str = "Taiwan Stock Accumulation Evidence"
    app_env: str = "development"
    database_url: str = "sqlite:///./accumulation.db"
    database_password_file: str | None = None
    database_host: str = "postgres"
    database_name: str = "accumulation"
    database_user: str = "accumulation"
    finmind_api_token: str | None = None
    finmind_base_url: str = "https://api.finmindtrade.com/api/v4"
    raw_root: Path = Path("data/raw")
    timezone: str = "Asia/Taipei"
    score_version: str = "s-only-v1"
    broker_concurrency: int = 4
    broker_rate_per_second: float = 4.0
    broker_max_retries: int = 4
    source_concurrency: int = 6
    source_rate_per_second: float = 4.0
    source_batch_size: int = 500
    worker_heartbeat_file: Path = Path("/data/raw/worker-heartbeat.json")
    allow_demo_data: bool = False

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    def resolved_database_url(self) -> str:
        if self.database_url and not self.database_url.startswith("sqlite"):
            return self.database_url
        if self.database_password_file:
            password_path = Path(self.database_password_file)
            if password_path.exists():
                password = password_path.read_text(encoding="utf-8").strip()
                return (
                    f"postgresql+psycopg://{self.database_user}:{password}"
                    f"@{self.database_host}:5432/{self.database_name}"
                )
        return self.database_url


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
