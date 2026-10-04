import os
from pathlib import Path
from typing import Optional
from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="allow")

    MONGODB_URL: str = "mongodb://127.0.0.1:27017"
    DATABASE_NAME: str = "dice_automation"
    RESUMES_DIR: Path = Path(__file__).resolve().parent.parent / "resumes"
    DATA_DIR: Path = Path(__file__).resolve().parent.parent / "data"

    # Browser infrastructure configuration
    BROWSER_MODE: str = "local"  # "local" or "server"
    HEADLESS: Optional[bool] = None
    HEADLESS_BROWSER: Optional[bool] = None  # backward-compatibility alias
    BROWSER_DATA_DIR: Optional[Path] = None

    # Session encryption
    SESSION_ENCRYPTION_KEY: str = ""

    # CORS configuration
    CORS_ORIGINS: str = "*"

    OPENAI_API_KEY: str = ""
    OPENAI_MODEL: str = "gpt-4o-mini"
    MAX_FILE_SIZE_MB: int = 20
    ALLOWED_EXTENSIONS: list[str] = [".pdf", ".docx"]


settings = Settings()

# Resolve default BROWSER_DATA_DIR
if settings.BROWSER_DATA_DIR is None:
    settings.BROWSER_DATA_DIR = settings.DATA_DIR / "browser_profiles"

# Auto-detect server mode when running in container/Render
if os.environ.get("RENDER") and "BROWSER_MODE" not in os.environ:
    settings.BROWSER_MODE = "server"

# Ensure directories exist
settings.RESUMES_DIR.mkdir(parents=True, exist_ok=True)
settings.DATA_DIR.mkdir(parents=True, exist_ok=True)
settings.BROWSER_DATA_DIR.mkdir(parents=True, exist_ok=True)

