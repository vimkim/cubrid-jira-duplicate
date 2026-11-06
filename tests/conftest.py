import json
import socket
from pathlib import Path

import pytest

from cbrd_dup.measure import gold


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """Fail any test that tries to open a socket. Tests use httpx.MockTransport or files instead."""

    def refuse(*args, **kwargs):
        raise AssertionError("tests must not touch the network")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket, "getaddrinfo", refuse)


@pytest.fixture(scope="session")
def gold_issues() -> dict:
    """Three duplicate issues and the candidates JIRA shows for them (CBRD-9999 is not visible)."""
    return json.loads((Path(__file__).parent / "fixtures" / "gold_issues.json").read_text(encoding="utf-8"))


@pytest.fixture
def gold_pairs(gold_issues):
    dups = gold_issues["duplicates"]
    candidates = {i["key"]: i for i in gold_issues["candidates"]}
    pointers = {issue["key"]: gold.collect_pointers(issue)[0] for issue in dups}
    return gold.build_pairs(dups, pointers, candidates)
