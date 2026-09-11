"""The one lock on the door: a single key in one header."""

import pytest
from fastapi import HTTPException

from app.api.routes import require_api_key
from app.settings import get_settings


@pytest.fixture
def api_key(monkeypatch):
    monkeypatch.setenv("WS_API_KEY", "k-test-123")
    get_settings.cache_clear()
    yield "k-test-123"
    get_settings.cache_clear()


async def test_missing_key_is_401(api_key):
    with pytest.raises(HTTPException) as exc:
        await require_api_key("")
    assert exc.value.status_code == 401


async def test_wrong_key_is_401(api_key):
    with pytest.raises(HTTPException) as exc:
        await require_api_key("k-wrong")
    assert exc.value.status_code == 401


async def test_right_key_passes(api_key):
    assert await require_api_key(api_key) is None


async def test_unset_key_rejects_everything(monkeypatch):
    # An empty configured key must mean "nobody", never "everybody".
    monkeypatch.setenv("WS_API_KEY", "")
    get_settings.cache_clear()
    try:
        with pytest.raises(HTTPException):
            await require_api_key("")
    finally:
        get_settings.cache_clear()
