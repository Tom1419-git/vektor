from functools import lru_cache
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_env: str = "production"
    database_url: str
    ollama_base_url: str = "http://host.docker.internal:11434"
    ollama_model: str = "llama3.1:latest"
    telegram_bot_token: str = ""
    telegram_allowed_user_ids: str = ""
    vektor_api_token: str
    public_base_url: str = ""
    alexa_skill_id: str = ""
    ollama_keep_alive: str = "-1"

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    @property
    def allowed_user_ids(self) -> set[int]:
        values = set()
        for item in self.telegram_allowed_user_ids.split(","):
            item = item.strip()
            if item:
                values.add(int(item))
        return values


@lru_cache
def get_settings() -> Settings:
    return Settings()
