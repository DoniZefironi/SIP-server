from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import URL


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="TELEPHONY_", extra="ignore")

    log_level: str = "INFO"
    auth_secure_cookies: bool = True

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

    # Записи разговоров и сообщения (их пишет Asterisk, сервис читает и чистит).
    recordings_dir: Path = Path("/srv/recordings")
    recordings_retention_days: int = 365
    events_retention_days: int = 90
    maintenance_enabled: bool = True
    maintenance_interval: float = 3600.0
    recording_queue_enabled: bool = True
    speech_provider: Literal["speechkit", "aiai"] = "speechkit"
    speechkit_enabled: bool = False
    speechkit_api_key: SecretStr = SecretStr("")
    summary_provider: Literal["yandex", "aiai"] = "yandex"
    yandexgpt_enabled: bool = False
    yandexgpt_folder_id: str = ""
    yandexgpt_api_key: SecretStr = SecretStr("")
    aiai_api_key: SecretStr = SecretStr("")
    aiai_base_url: str = "https://api.aiai.by/v1"
    aiai_model: str = "gemini-2.5-flash-lite"
    aiai_voice_stt_model: str = "nova-3"
    aiai_recording_stt_model: str = "whisper-1"
    aiai_tts_model: str = "eleven-flash-v2.5"
    aiai_tts_voice: str = "lily"
    bitrix_enabled: bool = False
    bitrix_directory_enabled: bool = False
    bitrix_contacts_enabled: bool = True
    bitrix_webhook_url: SecretStr = SecretStr("")
    bitrix_deal_code_field: str = "UF_CRM_1730705809359"
    bitrix_sector_recipients: dict[int, int] = {}
    bitrix_default_recipient: int | None = None
    savtassist_base_url: str = "https://helper.savt.by"
    savtassist_api_secret: SecretStr = SecretStr("")
    sector_names: dict[int, str] = {
        1: "Коммерческий",
        2: "Наладка",
        3: "Разработка ПО",
        4: "Проектирование",
        5: "Бухгалтерия",
    }
    sector_numbers: dict[int, str] = {}
    employee_names: dict[str, str] = {}

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
