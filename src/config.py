from functools import lru_cache
from pathlib import Path

import yaml
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parent.parent
PREFERENCES_PATH = REPO_ROOT / "config" / "preferences.yaml"
RESUME_PATH = REPO_ROOT / "profile" / "resume.tex"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=REPO_ROOT / ".env", extra="ignore")

    anthropic_api_key: str
    database_url: str
    # Optional: raises the GitHub REST API rate limit from 60/hr (unauthenticated)
    # to 5000/hr. Not required - the Resume Agent fetches only a handful of
    # repos once per pipeline run, well within the unauthenticated limit.
    github_token: str | None = None


@lru_cache
def get_settings() -> Settings:
    return Settings()


def load_preferences() -> dict:
    with open(PREFERENCES_PATH) as f:
        return yaml.safe_load(f)
