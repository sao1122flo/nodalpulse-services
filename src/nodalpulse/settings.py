from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    database_url: str

    @field_validator("database_url", mode="before")
    @classmethod
    def _fix_db_scheme(cls, v: str) -> str:
        """Railway's Postgres Reference resolves to postgresql:// (sync driver).
        The codebase uses async SQLAlchemy + asyncpg, so we rewrite the scheme
        at load time. Also handles Heroku-style postgres:// for defensive parity.
        Without this, services + worker + scheduler all crash with
        ModuleNotFoundError: No module named 'psycopg2' on boot."""
        for prefix in ("postgres://", "postgresql://"):
            if v.startswith(prefix):
                return "postgresql+asyncpg://" + v[len(prefix):]
        return v

    anthropic_api_key: str

    r2_access_key_id: str
    r2_secret_access_key: str
    r2_endpoint_url: str
    r2_bucket: str = "nodalpulse-docs"

    brevo_api_key: str = ""
    brevo_sender_email: str = "brief@nodalpulse.com"
    brevo_sender_name: str = "NodalPulse"
    app_url: str = "https://app.nodalpulse.com"

    services_api_key: str = ""

    sentry_dsn: str = ""
    log_level: str = "INFO"
    environment: str = "development"

    # Brief personalization
    max_lookback_days: int = 7


settings = Settings()
