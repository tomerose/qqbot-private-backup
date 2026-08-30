import importlib.util
import re
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


def load_public_proxy():
    spec = importlib.util.spec_from_file_location(
        "xiaoning_openai_proxy", ROOT / "openai_proxy.py"
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_public_proxy_removes_provider_specific_controls_and_uses_user_model():
    proxy = load_public_proxy()

    payload = proxy.build_chat_payload(
        {
            "model": "gemini-3.7-flash",
            "messages": [{"role": "user", "content": "hello"}],
            "google_search": True,
            "thinking": True,
            "model_role": "quality",
        },
        "user-owned-model",
    )

    assert payload["model"] == "user-owned-model"
    assert payload["messages"][0]["content"] == "hello"
    assert "google_search" not in payload
    assert "thinking" not in payload
    assert "model_role" not in payload


def test_public_proxy_rejects_missing_user_api_configuration(monkeypatch):
    proxy = load_public_proxy()
    monkeypatch.delenv("XIAONING_LLM_API_BASE", raising=False)
    monkeypatch.delenv("XIAONING_LLM_API_KEY", raising=False)
    monkeypatch.delenv("XIAONING_LLM_MODEL", raising=False)

    with pytest.raises(proxy.ConfigurationError):
        proxy.read_settings()


def test_public_install_and_setup_never_accept_api_key_as_command_line_input():
    install = (ROOT / "install.ps1").read_text(encoding="utf-8")
    setup = (ROOT / "setup.ps1").read_text(encoding="utf-8")

    assert "-AsSecureString" in setup
    assert not re.search(r"\[string\]\s*\$ApiKey", setup, re.IGNORECASE)
    assert "-ApiKey" not in install


def test_public_release_sources_have_no_known_machine_or_cloud_identity():
    paths = [
        ROOT / "README.md",
        ROOT / "BACKUP_SCOPE.md",
        ROOT / "gemini-proxy.py",
        ROOT / "image_proxy_core.py",
        ROOT / "openai_proxy.py",
        ROOT / "setup.ps1",
        ROOT / "start_all_services.ps1",
        ROOT / "xiaoning.local.example.ps1",
    ]
    text = "\n".join(path.read_text(encoding="utf-8") for path in paths)

    for marker in (
        "solar-modem-" + "496213-f5",
        "D:\\Claudecoda" + "学习",
        "C:\\Users\\" + "liu",
        "900000001",
        "900000002",
    ):
        assert marker not in text


def test_topics_file_contains_public_discovery_terms():
    topics = (ROOT / "github-topics.txt").read_text(encoding="utf-8")
    assert "astrbot" in topics
    assert "qqbot" in topics
    assert "self-hosted" in topics
    assert "xiaoning" in topics
