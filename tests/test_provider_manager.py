import asyncio

from mesh_ai_gateway.ai.base import AIProvider
from mesh_ai_gateway.ai.manager import ProviderManager
from mesh_ai_gateway.config import AIConfig, AIRequestConfig, ProviderConfig
from mesh_ai_gateway.errors import ProviderError


class FakeProvider(AIProvider):
    async def generate(self, messages):
        if self.config.host == "fail":
            raise ProviderError("nope")
        return f"reply from {self.config.provider_id}"

    async def healthcheck(self):
        return self.config.host != "fail", "ready" if self.config.host != "fail" else "nope"


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
