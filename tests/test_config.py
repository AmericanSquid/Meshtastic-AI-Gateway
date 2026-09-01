from pathlib import Path

import pytest

from mesh_ai_gateway.config import ConfigError, example_config, load_config
from mesh_ai_gateway.paths import default_socket_path, runtime_dir


def test_example_config_loads(tmp_path: Path):
    path = tmp_path / "config.yaml"
    path.write_text(example_config(), encoding="utf-8")
    config = load_config(path)
    assert config.mesh.transport == "tcp"
    assert config.ai.providers["local"].name == "Lil Local Guy"


def test_bad_transport_is_rejected(tmp_path: Path):
    path = tmp_path / "config.yaml"
    path.write_text(
        example_config().replace("transport: tcp", "transport: pigeon"), encoding="utf-8"
    )
    with pytest.raises(ConfigError):
        load_config(path)


def test_provider_enabled_requires_a_boolean(tmp_path: Path):
    path = tmp_path / "config.yaml"
    path.write_text(
        """ai:
  providers:
    disabled:
      type: hermes
      enabled: false
    enabled:
      type: hermes
      enabled: true
""",
        encoding="utf-8",
    )
    config = load_config(path)
    assert config.ai.providers["disabled"].enabled is False
    assert config.ai.providers["enabled"].enabled is True

    path.write_text(
        path.read_text(encoding="utf-8").replace("enabled: false", 'enabled: "false"'),
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="enabled must be true or false"):
        load_config(path)


def test_default_socket_uses_runtime_dir(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("XDG_RUNTIME_DIR", "/run/user/1234")
    assert runtime_dir() == Path("/run/user/1234/mesh-ai-gateway")
    assert default_socket_path() == Path("/run/user/1234/mesh-ai-gateway/control.sock")
