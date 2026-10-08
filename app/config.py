import os
from pathlib import Path
from typing import Optional
from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="allow")

    # Environment & App
    ENVIRONMENT: str = "development"
    CORS_ORIGINS: str = "*"

    # Database configuration (supports both MONGODB_DB and DATABASE_NAME)
    MONGODB_URL: str = "mongodb://127.0.0.1:27017"
    DATABASE_NAME: Optional[str] = None
    MONGODB_DB: Optional[str] = None
    RESUMES_DIR: Path = Path(__file__).resolve().parent.parent / "resumes"
    DATA_DIR: Path = Path(__file__).resolve().parent.parent / "data"

    # Browser infrastructure configuration
    BROWSER_MODE: str = "local"  # "local" or "server"
    HEADLESS: Optional[bool] = None
    HEADLESS_BROWSER: Optional[bool] = None  # backward-compatibility alias
    BROWSER_DATA_DIR: Optional[Path] = None

    # Session encryption
    SESSION_ENCRYPTION_KEY: str = ""

    OPENAI_API_KEY: str = ""
    OPENAI_MODEL: str = "gpt-4o-mini"
    MAX_FILE_SIZE_MB: int = 20
    ALLOWED_EXTENSIONS: list[str] = [".pdf", ".docx"]

    # Authentication & JWT (from bench-sales-backend)
    JWT_SECRET: str = "dice-auto-apply-default-jwt-secret-key-32bytes-min"
    JWT_ALGORITHM: str = "HS256"
    ACCESS_TOKEN_MINUTES: int = 60
    REFRESH_TOKEN_DAYS: int = 14
    COOKIE_SECURE: bool = False
    COOKIE_SAMESITE: str = "lax"
    GOOGLE_CLIENT_ID: str = ""
    GOOGLE_CLIENT_SECRET: str = ""

    # Cloudinary Cloud Storage (from bench-sales-backend)
    CLOUDINARY_CLOUD_NAME: str = ""
    CLOUDINARY_API_KEY: str = ""
    CLOUDINARY_API_SECRET: str = ""
    CLOUDINARY_URL: str = ""
    TEMP_RESUMES_DIR: Path = Path(__file__).resolve().parent.parent / "temp_resumes"

    # Backward-compatible property accessors for lowercase names
    @property
    def jwt_secret(self) -> str:
        return self.JWT_SECRET

    @property
    def jwt_algorithm(self) -> str:
        return self.JWT_ALGORITHM

    @property
    def access_token_minutes(self) -> int:
        return self.ACCESS_TOKEN_MINUTES

    @property
    def refresh_token_days(self) -> int:
        return self.REFRESH_TOKEN_DAYS

    @property
    def cookie_secure(self) -> bool:
        return self.COOKIE_SECURE

    @property
    def cookie_samesite(self) -> str:
        return self.COOKIE_SAMESITE

    @property
    def google_client_id(self) -> str:
        return self.GOOGLE_CLIENT_ID

    @property
    def cloudinary_cloud_name(self) -> str:
        return self.CLOUDINARY_CLOUD_NAME

    @property
    def cloudinary_api_key(self) -> str:
        return self.CLOUDINARY_API_KEY

    @property
    def cloudinary_api_secret(self) -> str:
        return self.CLOUDINARY_API_SECRET

    @property
    def cloudinary_url(self) -> str:
        return self.CLOUDINARY_URL

    @property
    def google_client_secret(self) -> str:
        return self.GOOGLE_CLIENT_SECRET

    @property
    def temp_resumes_dir(self) -> Path:
        return self.TEMP_RESUMES_DIR

    @property
    def mongodb_db(self) -> str:
        return self.DATABASE_NAME or self.MONGODB_DB or "apply2hire"

    @property
    def database_name(self) -> str:
        return self.mongodb_db

    @property
    def is_prod(self) -> bool:
        return self.ENVIRONMENT.lower() == "production"

    @property
    def origins(self) -> list[str]:
        return [o.strip() for o in self.CORS_ORIGINS.split(",") if o.strip()]


settings = Settings()

# Resolve default DATABASE_NAME (ensuring MONGODB_DB or DATABASE_NAME is respected)
if settings.DATABASE_NAME is None:
    settings.DATABASE_NAME = settings.MONGODB_DB or "apply2hire"
if settings.MONGODB_DB is None:
    settings.MONGODB_DB = settings.DATABASE_NAME

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
settings.TEMP_RESUMES_DIR.mkdir(parents=True, exist_ok=True)


