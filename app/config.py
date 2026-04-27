"""Centralized configuration via environment variables and .env file."""

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    debug: bool = False
    host: str = "0.0.0.0"
    port: int = 8000
    cors_origins: str = "http://localhost:8000,http://127.0.0.1:8000"

    # Spotify (optional — leave empty to disable)
    spotify_client_id: str = ""
    spotify_client_secret: str = ""

    # Presentation window (native Qt fullscreen)
    presentation_monitor: int = 0

    model_config = {
        "env_prefix": "DISCOBOT_",
        "env_file": ".env",
        "env_file_encoding": "utf-8",
    }


settings = Settings()
