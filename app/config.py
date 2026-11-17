from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import URL


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="TELEPHONY_", extra="ignore")

    log_level: str = "INFO"

    db_host: str = "127.0.0.1"
    db_port: int = 5432
    db_name: str = "telephony"
    db_user: str = "telephony"
    db_password: str = ""

    ami_enabled: bool = True
    ami_host: str = "127.0.0.1"
    ami_port: int = 5038
    ami_username: str = "telephony"
    ami_secret: str = ""
    ami_reconnect_delay: float = 5.0
    ami_ping_interval: float = 20.0

    @property
    def database_url(self) -> URL:
        return URL.create(
            "postgresql+asyncpg",
            username=self.db_user,
            password=self.db_password,
            host=self.db_host,
            port=self.db_port,
            database=self.db_name,
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()
