from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from ..paths import env_file_path, systemd_unit_path

SERVICE = "mesh-ai-gateway.service"


def _run(*args: str, check: bool = False) -> subprocess.CompletedProcess:
    command = ["systemctl", *args]
    try:
        return subprocess.run(
            command,
            text=True,
            capture_output=True,
            check=check,
        )
    except FileNotFoundError as exc:
        return subprocess.CompletedProcess(command, 127, stdout="", stderr=str(exc))


def _unit_quote(value: str | Path) -> str:
    text = str(value).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{text}"'


def unit_text(config_path: Path, user: str | None = None) -> str:
    user_line = f"User={user}\n" if user else ""
    python = _unit_quote(Path(sys.executable).resolve())
    config = _unit_quote(config_path.expanduser().resolve())
    env_file = env_file_path(config_path).expanduser()

    return f"""[Unit]
Description=Meshtastic AI Gateway
Wants=network-online.target
After=network-online.target

[Service]
Type=simple
{user_line}ExecStart={python} -m mesh_ai_gateway daemon --config {config}
Restart=on-failure
RestartSec=3
EnvironmentFile=-{env_file}
RuntimeDirectory=mesh-ai-gateway
RuntimeDirectoryMode=0700

[Install]
WantedBy=default.target
"""


def install(
    config_path: Path,
    system: bool = False,
    user: str | None = None,
) -> Path:
    if system:
        unit = Path("/etc/systemd/system/mesh-ai-gateway.service")
    else:
        unit = systemd_unit_path()

    unit.parent.mkdir(parents=True, exist_ok=True)

    unit.write_text(
        unit_text(
            config_path,
            user=user if system else None,
        ),
        encoding="utf-8",
    )

    env = env_file_path(config_path)
    env.parent.mkdir(parents=True, exist_ok=True)

    if not env.exists():
        env.write_text(
            "# HF_TOKEN=...\n# NCLOUD_API_KEY=...\n",
            encoding="utf-8",
        )
        env.chmod(0o600)

    if system:
        result = subprocess.run(
            ["systemctl", "daemon-reload"],
            capture_output=True,
            text=True,
        )
    else:
        result = _run("daemon-reload")

    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "systemctl daemon-reload failed")

    return unit


def action(name: str) -> tuple[bool, str]:
    if name not in {"start", "stop", "restart", "enable", "disable"}:
        raise ValueError(name)
    args = [name]
    if name == "enable":
        args.append("--now")
    result = _run(*args, SERVICE)
    text = (result.stdout or result.stderr).strip()
    return result.returncode == 0, text


def status() -> dict:
    active = _run("is-active", SERVICE)
    enabled = _run("is-enabled", SERVICE)
    return {
        "active": active.stdout.strip() or "unknown",
        "enabled": enabled.stdout.strip() or "unknown",
        "running": active.returncode == 0,
    }
