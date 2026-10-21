"""The local verifier cannot follow a response to a different origin."""

from unittest.mock import Mock
from urllib.request import ProxyHandler, Request

import pytest

from scripts import check_preparation_http as checker


def test_checker_refuses_redirect_without_making_followup_request(monkeypatch):
    assert checker.NoRedirect().redirect_request(
        Request("http://127.0.0.1:8011/health"), None, 307,
        "redirect", {}, "https://example.invalid/collect",
    ) is None
    response = Mock(status=307)
    response.__enter__ = Mock(return_value=response)
    response.__exit__ = Mock(return_value=False)
    opener = Mock()
    opener.open.return_value = response
    monkeypatch.setattr(checker, "_OPENER", opener)
    with pytest.raises(RuntimeError, match="refuses redirects"):
        checker.call("http://127.0.0.1:8011", "/health")
    assert opener.open.call_count == 1
    response.read.assert_not_called()


def test_checker_has_no_environment_proxy_handlers():
    assert not any(isinstance(handler, ProxyHandler) and handler.proxies
                   for handler in checker._OPENER.handlers)
