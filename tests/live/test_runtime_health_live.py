"""Explicit local-runtime probes; excluded from PR/offline test runs."""

from __future__ import annotations

import json
from urllib.request import urlopen

import pytest


pytestmark = pytest.mark.live


def _json(url: str) -> dict:
    with urlopen(url, timeout=8) as response:  # noqa: S310 - fixed loopback URL
        return json.loads(response.read().decode("utf-8"))


def test_gemini_proxy_deep_health():
    payload = _json("http://127.0.0.1:3000/healthz?deep=true")
    assert payload.get("ok") is True
    assert payload.get("upstream") == "ok"


def test_local_tts_health():
    payload = _json("http://127.0.0.1:8766/health")
    assert payload.get("status") == "ok"
    assert payload.get("engines")
