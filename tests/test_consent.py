"""Consent handling must not scan the whole SERP when no dialog is present."""

import pytest

from app.search.google import _dismiss_consent


class _Count:
    def __init__(self, n):
        self._n = n

    async def count(self):
        return self._n


class _FakePage:
    """Only what _dismiss_consent touches. get_by_role is the expensive path."""

    def __init__(self, url, markers):
        self.url = url
        self._markers = markers
        self.role_lookups = 0

    def locator(self, selector):
        return _Count(self._markers)

    def get_by_role(self, role, name=None):
        self.role_lookups += 1
        return _Count(0)


async def test_no_dialog_means_no_role_scan():
    page = _FakePage("https://www.google.com/search?q=x", markers=0)
    await _dismiss_consent(page)
    assert page.role_lookups == 0


async def test_marker_present_runs_the_click_path():
    page = _FakePage("https://www.google.com/search?q=x", markers=1)
    await _dismiss_consent(page)
    assert page.role_lookups == 3          # every label tried, none matched


async def test_consent_host_runs_the_click_path():
    page = _FakePage("https://consent.google.com/m?continue=...", markers=0)
    await _dismiss_consent(page)
    assert page.role_lookups == 3
