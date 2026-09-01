from pathlib import Path

from mesh_ai_gateway.service import systemd


def test_unit_uses_current_python_and_quotes_config_path(monkeypatch):
    monkeypatch.setattr(systemd.sys, "executable", "/opt/venv/bin/python")

    unit = systemd.unit_text(Path("/tmp/mesh config.yaml"))

    assert (
        'ExecStart="/opt/venv/bin/python" -m mesh_ai_gateway daemon --config "/tmp/mesh config.yaml"'
        in unit
    )
    assert "EnvironmentFile=-/tmp/env" in unit
