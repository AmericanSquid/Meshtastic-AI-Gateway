import asyncio

from mesh_ai_gateway.ai.base import AIProvider
from mesh_ai_gateway.ai.manager import ProviderManager
from mesh_ai_gateway.config import AIConfig, BridgeConfig, ProviderConfig
from mesh_ai_gateway.mesh.manager import MeshMessage
from mesh_ai_gateway.router import MessageRouter


class FakeProvider(AIProvider):
    async def generate(self, messages):
        return "ok"

    async def healthcheck(self):
        return True, "ready"


class FakeMesh:
    def snapshot(self):
        return {"status": "connected", "transport": "tcp"}


def test_router_delegates_reset_status_and_provider_commands():
    providers = ProviderManager(
        AIConfig(
            providers={
                "first": ProviderConfig(
                    "first", "First", "ollama", priority=1, host="ok", model="x"
                ),
                "second": ProviderConfig(
                    "second", "Second", "ollama", priority=2, host="ok", model="x"
                ),
            }
        ),
        provider_factory=FakeProvider,
    )
    router = MessageRouter(FakeMesh(), providers, BridgeConfig())
    router.sessions.append("!node", "user", "remember me")

    async def commands():
        reset = await router._command(MeshMessage("!reset", "!node", 0))
        status = await router._command(MeshMessage("!status", "!node", 0))
        provider = await router._command(MeshMessage("!provider second", "!node", 0))
        return reset, status, provider

    reset, status, provider = asyncio.run(commands())

    assert reset == "Conversation reset."
    assert router.sessions.get("!node") == []
    assert status == "Mesh: connected (tcp). AI: auto."
    assert provider == "Provider: second. Routing: manual."
    assert providers.manual_provider == "second"
