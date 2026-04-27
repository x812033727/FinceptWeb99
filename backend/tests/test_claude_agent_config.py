"""Pure unit tests for `Settings.claude_agent_effective_enabled`.

The flag now ANDs `CLAUDE_AGENT_ENABLED` with importability of the
optional `claude_agent_sdk` package. Operators who omit the SDK get a
graceful 503 in the router instead of a 500 at import time.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

from config import Settings


def _settings(**kw) -> Settings:
    base = {
        "JWT_SECRET_KEY": "x" * 32,
        "DEBUG": True,
    }
    base.update(kw)
    return Settings(**base)


def test_effective_enabled_false_when_flag_off():
    s = _settings(CLAUDE_AGENT_ENABLED=False)
    assert s.claude_agent_effective_enabled is False


def test_effective_enabled_false_when_sdk_missing():
    """Flag on, SDK missing ⇒ effective False (no crash)."""
    s = _settings(CLAUDE_AGENT_ENABLED=True)
    with patch("importlib.util.find_spec", return_value=None):
        assert s.claude_agent_effective_enabled is False


def test_effective_enabled_true_when_flag_on_and_sdk_present():
    s = _settings(CLAUDE_AGENT_ENABLED=True)
    fake_spec = object()
    with patch("importlib.util.find_spec", return_value=fake_spec):
        assert s.claude_agent_effective_enabled is True


def test_webfetch_hosts_default_includes_curated_set():
    s = _settings()
    hosts = s.claude_agent_webfetch_hosts
    assert "raw.githubusercontent.com" in hosts
    assert "docs.anthropic.com" in hosts
    assert "api.stlouisfed.org" in hosts


def test_webfetch_hosts_lowercased_and_trimmed():
    s = _settings(CLAUDE_AGENT_WEBFETCH_ALLOWLIST="EXAMPLE.COM,  Foo.org , bar.io")
    assert s.claude_agent_webfetch_hosts == ["example.com", "foo.org", "bar.io"]


def test_webfetch_hosts_empty_when_explicitly_blanked():
    s = _settings(CLAUDE_AGENT_WEBFETCH_ALLOWLIST="")
    assert s.claude_agent_webfetch_hosts == []


@pytest.mark.parametrize("raw", ["", "   "])
def test_update_command_argv_empty_when_blank(raw: str):
    s = _settings(UPDATE_COMMAND=raw)
    assert s.update_command_argv == []


def test_update_command_argv_parses_json_list():
    s = _settings(UPDATE_COMMAND='["docker","compose","pull"]')
    assert s.update_command_argv == ["docker", "compose", "pull"]


def test_update_command_argv_rejects_non_list():
    s = _settings(UPDATE_COMMAND='"not a list"')
    with pytest.raises(ValueError):
        _ = s.update_command_argv
