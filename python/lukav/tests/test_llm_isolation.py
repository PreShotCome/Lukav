"""Lukav must NOT silently inherit Theo's LLM_BACKEND env var."""
from __future__ import annotations

from lukav.llm import build_default_client, describe_default_backend


def test_bare_llm_backend_env_is_ignored(monkeypatch):
    # Simulate Theo's environment leaking through.
    monkeypatch.setenv("LLM_BACKEND", "ollama")
    monkeypatch.delenv("LUKAV_LLM_BACKEND", raising=False)
    info = describe_default_backend()
    assert info["chosen_backend"] == "claude"
    assert info["LLM_BACKEND_env_present_but_ignored"] == "ollama"


def test_lukav_namespaced_env_is_honored(monkeypatch):
    monkeypatch.setenv("LUKAV_LLM_BACKEND", "ollama")
    info = describe_default_backend()
    assert info["chosen_backend"] == "ollama"


def test_none_disables_llm(monkeypatch):
    monkeypatch.setenv("LUKAV_LLM_BACKEND", "none")
    assert build_default_client() is None


def test_anthropic_backend_requires_sdk(monkeypatch):
    monkeypatch.setenv("LUKAV_LLM_BACKEND", "anthropic")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    # If the SDK isn't installed in this environment, build raises.
    # If it is installed but no key, build raises for missing key.
    import pytest
    with pytest.raises(RuntimeError):
        build_default_client()


def test_unknown_backend_raises(monkeypatch):
    monkeypatch.setenv("LUKAV_LLM_BACKEND", "gpt-banana")
    import pytest
    with pytest.raises(ValueError):
        build_default_client()
