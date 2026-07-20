"""Application configuration.

All values come from environment variables (Railway) or backend/.env (local dev).
Secrets never have defaults; missing required config must fail at startup, loudly —
a worker that starts half-configured is a worker that trades blind.
"""

from enum import StrEnum

from pydantic_settings import BaseSettings, SettingsConfigDict


class WebullEnv(StrEnum):
    PAPER = "paper"
    LIVE = "live"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    # Webull OpenAPI
    webull_app_key: str
    webull_app_secret: str
    webull_env: WebullEnv = WebullEnv.PAPER  # paper unless explicitly promoted
    # Paper-trading API host (from the Webull developer portal). The SDK ships
    # only LIVE endpoint hosts, so when webull_env == paper the client wrapper
    # MUST route requests to this host and refuses to fall back to live (fail
    # closed). May be blank until the owner pastes the portal value into .env;
    # the wrapper raises a typed config error if a paper client is built while
    # it is still blank. Env var: WEBULL_PAPER_API_ENDPOINT.
    webull_paper_api_endpoint: str | None = None

    # Anthropic (research pipeline only — never imported by the order path)
    anthropic_api_key: str

    # Supabase / Postgres
    supabase_url: str
    supabase_anon_key: str
    supabase_service_role_key: str
    database_url: str


def load_settings() -> Settings:
    """Load and validate config. Raises on any missing required value (fail closed)."""
    return Settings()
