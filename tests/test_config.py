from pathlib import Path

import pytest

from mesh_ai_gateway.config import ConfigError, example_config, load_config


def test_example_config_loads(tmp_path: Path):
    path = tmp_path / "config.yaml"
    path.write_text(example_config(), encoding="utf-8")
    config = load_config(path)
    assert config.mesh.transport == "tcp"
    assert config.ai.providers["local"].name == "Lil Local Guy"


def test_bad_transport_is_rejected(tmp_path: Path):
    path = tmp_path / "config.yaml"
    path.write_text(example_config().replace("transport: tcp", "transport: pigeon"), encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(path)
