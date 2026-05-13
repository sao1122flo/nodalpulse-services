from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    database_url: str
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


settings = Settings()
