"""PERF-3: the bedrock-runtime boto3 client is cached and reused per key."""

from __future__ import annotations

import boto3
import pytest

import app.services.bedrock_generator as bg
from app.services.bedrock_generator import BedrockGenerator


@pytest.fixture(autouse=True)
def _clear_client_cache():
    """Each test starts with an empty module-level client cache."""
    bg._CLIENT_CACHE.clear()
    yield
    bg._CLIENT_CACHE.clear()


class _FakeClient:
    def __init__(self, **kwargs):
        self.kwargs = kwargs


def test_same_region_timeout_builds_one_shared_client(monkeypatch) -> None:
    calls: list[dict] = []

    def fake_client(service_name, **kwargs):
        calls.append({"service": service_name, **kwargs})
        return _FakeClient(**kwargs)

    monkeypatch.setattr(boto3, "client", fake_client)

    gen_a = BedrockGenerator("test-model", region_name="us-east-1")
    gen_b = BedrockGenerator("test-model", region_name="us-east-1")

    client_a = gen_a._get_client()
    client_b = gen_b._get_client()

    # Two generators with the same (region, timeout) key reuse ONE boto3 client.
    assert client_a is client_b
    assert len(calls) == 1
    assert calls[0]["service"] == "bedrock-runtime"
    assert calls[0]["region_name"] == "us-east-1"
    # The per-attempt botocore Config (timeout + disabled retries) is preserved.
    config = calls[0]["config"]
    assert config.connect_timeout == pytest.approx(30.0)
    assert config.read_timeout == pytest.approx(30.0)
    assert config.retries == {"max_attempts": 0, "mode": "standard"}


def test_injected_client_bypasses_cache(monkeypatch) -> None:
    calls: list[dict] = []

    def fake_client(service_name, **kwargs):
        calls.append({"service": service_name, **kwargs})
        return _FakeClient(**kwargs)

    monkeypatch.setattr(boto3, "client", fake_client)

    injected = object()
    gen = BedrockGenerator("test-model", client=injected)

    assert gen._get_client() is injected
    # An injected client never triggers boto3.client and never populates cache.
    assert calls == []
    assert bg._CLIENT_CACHE == {}


def test_different_timeout_builds_distinct_clients(monkeypatch) -> None:
    calls: list[dict] = []

    def fake_client(service_name, **kwargs):
        calls.append(kwargs)
        return _FakeClient(**kwargs)

    monkeypatch.setattr(boto3, "client", fake_client)

    gen_fast = BedrockGenerator(
        "test-model", region_name="us-east-1", attempt_timeout_ms=1000
    )
    gen_slow = BedrockGenerator(
        "test-model", region_name="us-east-1", attempt_timeout_ms=5000
    )

    assert gen_fast._get_client() is not gen_slow._get_client()
    assert len(calls) == 2
