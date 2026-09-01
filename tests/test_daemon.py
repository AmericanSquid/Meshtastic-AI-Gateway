import asyncio
from pathlib import Path

import pytest

from mesh_ai_gateway.config import example_config
from mesh_ai_gateway.daemon import GatewayDaemon


def test_failed_reload_restores_mesh_and_keeps_previous_config(tmp_path: Path, monkeypatch):
    path = tmp_path / "config.yaml"
    path.write_text(example_config(), encoding="utf-8")
    daemon = GatewayDaemon(path)
    previous_config = daemon.config
    previous_mesh_config = daemon.mesh.config
    previous_router_config = daemon.router.config
    previous_provider = daemon.providers.providers["local"]
    path.write_text(
        example_config().replace("history_messages: 12", "history_messages: 13"),
        encoding="utf-8",
    )
    original_reconfigure = daemon.router.reconfigure

    async def fail_router_reconfigure(config):
        await original_reconfigure(config)
        if config is previous_config.bridge:
            return
        raise RuntimeError("router failed")

    monkeypatch.setattr(daemon.router, "reconfigure", fail_router_reconfigure)

    with pytest.raises(RuntimeError, match="router failed"):
        asyncio.run(daemon.reload_config())

    assert daemon.config is previous_config
    assert daemon.mesh.config is previous_mesh_config
    assert daemon.router.config is previous_router_config
    assert daemon.providers.providers["local"] is previous_provider
