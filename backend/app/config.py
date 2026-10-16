"""Конфигурация приложения (все секреты и настройки — из окружения, §48)."""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- База данных ---
    database_url: str = ""
    postgres_user: str = "culturalfind"
    postgres_password: str = "culturalfind"
    postgres_db: str = "culturalfind"
    postgres_host: str = "db"
    postgres_port: int = 5432

    # --- AI / NLP (§4: модель можно заменить без изменения архитектуры) ---
    ai_enabled: bool = True
    ai_base_url: str = "http://localhost:11434"
    ai_model: str = "qwen2.5:7b"
    embedding_model: str = "nomic-embed-text"
    ai_timeout_seconds: float = 60.0
    # агент с function calling (идея 10): многошаговые вопросы через инструменты;
    # при любом сбое — тихий откат на обычный одиночный разбор (§53)
    ai_agent_enabled: bool = True
    ai_agent_max_steps: int = 5
    # LLM-резюме ответа (идея 16) и стриминг (идея 18): живой текст вместо
    # шаблонного объяснения; при сбое — шаблон; у резюме свой короткий таймаут
    ai_summary_enabled: bool = True
    ai_summary_timeout_seconds: float = 20.0

    # --- Картография / геокодирование ---
    map_tile_url: str = "https://tile.openstreetmap.org/{z}/{x}/{y}.png"
    geocoder_url: str = ""  # внешний геокодер — только fallback (§26)

    # --- Ограничения безопасности (§42) ---
    max_radius_meters: float = 10000.0
    max_limit: int = 100

    # --- Веса гибридного ранжирования (§28) — не зашивать в код ---
    score_weight_semantic: float = 0.5
    score_weight_spatial: float = 0.3
    score_weight_category: float = 0.2

    # --- Эмбеддинги ---
    embedding_dim: int = 768  # nomic-embed-text

    # --- Прочее ---
    log_level: str = "INFO"

    @property
    def db_url(self) -> str:
        """Итоговый SQLAlchemy URL (DATABASE_URL имеет приоритет)."""
        if self.database_url:
            return self.database_url
        return (
            f"postgresql+psycopg://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()
