from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from .config import ConfigError, example_config, load_config
from .daemon import run_daemon
from .ipc.client import request
from .paths import default_config_path
from .service import systemd


def _config_path(value: str | None) -> Path:
    return Path(value).expanduser() if value else default_config_path()


def config_init(path: Path, force: bool = False) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and not force:
        print(f"Config already exists: {path}", file=sys.stderr)
        return 1
    path.write_text(example_config(), encoding="utf-8")
    print(path)
    return 0


def print_status(config_path: Path) -> int:
    try:
        config = load_config(config_path)
        result = asyncio.run(request(config.ipc.socket_path, {"command": "status"}))
        print(json.dumps(result, indent=2))
        return 0 if result.get("ok") else 1
    except Exception as exc:
        print(f"Daemon unavailable: {exc}", file=sys.stderr)
        state = systemd.status()
        print(json.dumps({"systemd": state}, indent=2))
        return 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="mesh-ai-gateway")
    sub = parser.add_subparsers(dest="command", required=True)

    daemon = sub.add_parser("daemon", help="Run the gateway in the foreground")
    daemon.add_argument("--config")

    tui = sub.add_parser("tui", help="Attach the curses control interface")
    tui.add_argument("--config")

    status = sub.add_parser("status", help="Show daemon status as JSON")
    status.add_argument("--config")

    config = sub.add_parser("config", help="Manage YAML configuration")
    config_sub = config.add_subparsers(dest="config_command", required=True)
    init = config_sub.add_parser("init", help="Create an example config")
    init.add_argument("--config")
    init.add_argument("--force", action="store_true")
    check = config_sub.add_parser("check", help="Validate config")
    check.add_argument("--config")
    path = config_sub.add_parser("path", help="Print config path")
    path.add_argument("--config")

    providers = sub.add_parser("providers", help="Provider operations")
    providers_sub = providers.add_subparsers(dest="providers_command", required=True)
    test = providers_sub.add_parser("test", help="Health-check providers through the daemon")
    test.add_argument("--config")

    service = sub.add_parser("service", help="Manage the systemd user service")
    service_sub = service.add_subparsers(dest="service_command", required=True)
    install = service_sub.add_parser("install")
    install.add_argument("--config")
    install.add_argument(
        "--system",
        action="store_true",
        help="Install to /etc/systemd/system instead of the user service directory",
    )
    install.add_argument(
        "--user",
        help="User account to run the system service as",
    )

    for name in ("start", "stop", "restart", "enable", "disable", "status"):
        service_sub.add_parser(name)

    return parser


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "config":
            path = _config_path(args.config)
            if args.config_command == "init":
                raise SystemExit(config_init(path, args.force))
            if args.config_command == "path":
                print(path)
                return
            load_config(path)
            print(f"OK: {path}")
            return

        if args.command == "daemon":
            run_daemon(_config_path(args.config))
            return

        if args.command == "tui":
            path = _config_path(args.config)
            config = load_config(path)
            from .tui import run_tui

            run_tui(config.ipc.socket_path, path)
            return

        if args.command == "status":
            raise SystemExit(print_status(_config_path(args.config)))

        if args.command == "providers":
            path = _config_path(args.config)
            config = load_config(path)
            result = asyncio.run(
                request(config.ipc.socket_path, {"command": "test_providers"}, timeout=15.0)
            )
            print(json.dumps(result, indent=2))
            raise SystemExit(0 if result.get("ok") else 1)

        if args.command == "service":
            if args.service_command == "install":
                path = _config_path(args.config)
                load_config(path)
                unit = systemd.install(path, system=args.system, user=args.user)
                print(f"Installed {unit}")
                print("Run: mesh-ai-gateway service enable")
                return
            if args.service_command == "status":
                print(json.dumps(systemd.status(), indent=2))
                return
            ok, detail = systemd.action(args.service_command)
            if detail:
                print(detail)
            raise SystemExit(0 if ok else 1)
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
    except KeyboardInterrupt:
        raise SystemExit(130)
