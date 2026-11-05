"""Application settings, loaded from environment / .env."""

from functools import lru_cache
from typing import Annotated

from pydantic import field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Application
    PROJECT_NAME: str = "Corman CMS"
    ENVIRONMENT: str = "development"
    DEBUG: bool = True
    # Echo every SQL statement to the log. Off by default: with DEBUG on this used
    # to log ~25 statements per dashboard load, which both slowed the request and
    # flooded the log. Turn on deliberately when debugging a specific query.
    SQL_ECHO: bool = False
    API_V1_PREFIX: str = "/api/v1"

    # Security
    SECRET_KEY: str = "change-me"
    ALGORITHM: str = "HS256"
    # A full working day, so a session does not lapse mid-use. When it does expire,
    # the frontend silently exchanges the refresh token for a new pair (see api.ts).
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 480
    REFRESH_TOKEN_EXPIRE_DAYS: int = 30

    # Database
    # No default: the connection string contains credentials, so it must be
    # provided at runtime via the DATABASE_URL environment variable (or .env) and
    # is never hard-coded here. See .env.example for the expected format.
    DATABASE_URL: str

    # CORS
    # NoDecode: keep the raw string so the validator below can split on commas.
    CORS_ORIGINS: Annotated[list[str], NoDecode] = ["http://localhost:4300"]

    @field_validator("CORS_ORIGINS", mode="before")
    @classmethod
    def split_origins(cls, v: str | list[str]) -> list[str]:
        if isinstance(v, str):
            return [origin.strip() for origin in v.split(",") if origin.strip()]
        return v


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
