import asyncio
from pathlib import Path

from mesh_ai_gateway.ai.base import AIProvider
from mesh_ai_gateway.ai.manager import ProviderManager
from mesh_ai_gateway.ai.providers import HermesProvider
from mesh_ai_gateway.config import AIConfig, AIRequestConfig, ProviderConfig
from mesh_ai_gateway.errors import ProviderError


class FakeProvider(AIProvider):
    def __init__(self, config):
        super().__init__(config)
        self.closed = False

    async def generate(self, messages):
        if self.config.host == "fail":
            raise ProviderError("nope")
        return f"reply from {self.config.provider_id}"

    async def healthcheck(self):
        return self.config.host != "fail", "ready" if self.config.host != "fail" else "nope"

    async def aclose(self):
        self.closed = True


def test_provider_fallback():
    configs = {
        "first": ProviderConfig("first", "First", "ollama", priority=1, host="fail", model="x"),
        "second": ProviderConfig("second", "Second", "ollama", priority=2, host="ok", model="x"),
    }
    manager = ProviderManager(
        AIConfig(request=AIRequestConfig(attempts_per_provider=1, timeout=1), providers=configs),
        provider_factory=FakeProvider,
    )
    text, provider_id = asyncio.run(manager.generate([{"role": "user", "content": "hi"}]))
    assert provider_id == "second"
    assert "second" in text


def test_reconfigure_closes_replaced_providers():
    initial = AIConfig(
        providers={"first": ProviderConfig("first", "First", "ollama", host="ok", model="x")}
    )
    manager = ProviderManager(initial, provider_factory=FakeProvider)
    previous = manager.providers["first"]
    updated = AIConfig(
        providers={"second": ProviderConfig("second", "Second", "ollama", host="ok", model="y")}
    )

    asyncio.run(manager.reconfigure(updated))

    assert previous.closed is True
    assert list(manager.providers) == ["second"]


def test_hermes_healthcheck_accepts_executable_path(tmp_path: Path):
    command = tmp_path / "hermes"
    command.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    command.chmod(0o755)
    provider = HermesProvider(ProviderConfig("hermes", "Hermes", "hermes", command=str(command)))

    ok, detail = asyncio.run(provider.healthcheck())

    assert ok is True
    assert detail == str(command.resolve())
