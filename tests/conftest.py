import pytest

from src.config import get_settings


@pytest.fixture(autouse=True)
def _dummy_settings(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://localhost:5432/test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()
