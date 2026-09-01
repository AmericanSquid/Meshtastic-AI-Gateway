from __future__ import annotations

import os
from pathlib import Path

APP_NAME = "mesh-ai-gateway"


def config_dir() -> Path:
    root = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return root / APP_NAME


def default_config_path() -> Path:
    return config_dir() / "config.yaml"

def env_file_path(config_path: Path | None = None) -> Path:
    if config_path is not None:
        return config_path.parent / "env"
    return config_dir() / "env"

def runtime_dir() -> Path:
    raw = os.environ.get("XDG_RUNTIME_DIR")
    if raw:
        return Path(raw) / APP_NAME
    return Path("/tmp") / f"{APP_NAME}-{os.getuid()}"

def default_socket_path() -> Path:
    return Path("/run/mesh-ai-gateway/control.sock")

def systemd_user_dir() -> Path:
    return config_dir().parent / "systemd" / "user"


def systemd_unit_path() -> Path:
    return systemd_user_dir() / f"{APP_NAME}.service"
