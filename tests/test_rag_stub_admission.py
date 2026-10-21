"""An explicit offline backend must not initialize installed live backends."""

import sys
from types import SimpleNamespace
from unittest.mock import Mock

from common.rag import DomainRAG


def test_explicit_stub_never_opens_chroma(monkeypatch):
    monkeypatch.setenv("RAG_BACKEND", "stub")
    collection = SimpleNamespace(count=lambda: 1)
    client = SimpleNamespace(get_or_create_collection=lambda name: collection)
    open_chroma = Mock(return_value=client)
    monkeypatch.setitem(sys.modules, "chromadb", SimpleNamespace(PersistentClient=open_chroma))
    rag = DomainRAG("housing")
    assert rag.backend == "stub"
    assert rag.is_real is False
    open_chroma.assert_not_called()
