from __future__ import annotations

import asyncio
import logging
import os
import signal
from pathlib import Path

from .ai.manager import ProviderManager
from .config import AppConfig, load_config
from .ipc.server import IPCServer
from .logging_setup import RingBufferHandler, configure_logging
from .mesh.manager import MeshManager
from .router import MessageRouter

log = logging.getLogger(__name__)


class GatewayDaemon:
    def __init__(self, config_path: str | Path):
        self.config_path = Path(config_path).expanduser()
        self.config: AppConfig = load_config(self.config_path)
        self.ring: RingBufferHandler = configure_logging(
            self.config.logging.level,
            self.config.logging.retain_lines,
        )
        self.providers = ProviderManager(self.config.ai)

        self.mesh = MeshManager(
            self.config.mesh,
            self._on_mesh_message,
        )
        self.router = MessageRouter(
            self.mesh,
            self.providers,
            self.config.bridge,
        )
        self.ipc = IPCServer(
            self.config.ipc.socket_path,
            self._handle_ipc,
        )
        self.stop_event = asyncio.Event()
        self.started_at: float | None = None
        self.mesh_task: asyncio.Task | None = None

    async def _on_mesh_message(self, message) -> None:
        await self.router.enqueue(message)

    async def reload_config(self) -> dict:
        candidate = load_config(self.config_path)
        previous = self.config
        old_socket = previous.ipc.socket_path
        prepared_providers = self.providers.prepare_reconfigure(candidate.ai)

        try:
            await self.mesh.reconfigure(candidate.mesh)
            await self.router.reconfigure(candidate.bridge)
        except Exception:
            await self.providers.discard_prepared(prepared_providers)
            await self.mesh.reconfigure(previous.mesh)
            await self.router.reconfigure(previous.bridge)
            raise

        previous_providers = self.providers.providers
        await self.providers.close_providers(previous_providers)
        self.providers.apply_prepared(candidate.ai, prepared_providers)
        self.config = candidate

        logging.getLogger().setLevel(
            getattr(
                logging,
                candidate.logging.level.upper(),
                logging.INFO,
            )
        )

        note = None
        if candidate.ipc.socket_path != old_socket:
            note = "ipc.socket_path changed; restart daemon to move the live socket"

        log.info(
            "Configuration reloaded from %s",
            self.config_path,
        )
        return {
            "ok": True,
            "note": note,
        }

    def snapshot(self) -> dict:
        import time

        uptime = int(time.time() - self.started_at) if self.started_at else 0

        return {
            "ok": True,
            "pid": os.getpid(),
            "uptime_seconds": uptime,
            "config": str(self.config_path),
            "mesh": self.mesh.snapshot(),
            "ai": {
                **self.providers.snapshot(),
                "hermes_enabled": (self.router.hermes is not None),
                "hermes_selected": (self.router.hermes_selected),
            },
            "queue": {
                "size": self.router.queue.qsize(),
                "capacity": self.router.queue.maxsize,
            },
        }

    async def _handle_ipc(
        self,
        request: dict,
    ) -> dict:
        command = request.get(
            "command",
            "status",
        )

        if command == "status":
            return self.snapshot()

        if command == "logs":
            return {
                "ok": True,
                "lines": self.ring.recent(int(request.get("limit", 100))),
            }

        if command == "nodes":
            return {
                "ok": True,
                "nodes": self.mesh.nodes(),
            }

        if command == "providers":
            return {
                "ok": True,
                **self.providers.snapshot(),
            }

        if command == "test_providers":
            return {
                "ok": True,
                "results": (await self.providers.test_all()),
            }

        if command == "next_detection_sensor":
            try:
                timeout = float(request.get("timeout", 5.0))
            except (TypeError, ValueError):
                return {
                    "ok": False,
                    "error": "timeout must be a number",
                }

            if timeout <= 0:
                return {
                    "ok": False,
                    "error": ("timeout must be greater than zero"),
                }

            try:
                packet = await self.mesh.next_detection_sensor_packet(timeout)
            except asyncio.TimeoutError:
                return {
                    "ok": False,
                    "error": "timeout",
                }

            return {
                "ok": True,
                "packet": packet,
            }

        if command == "send_mesh_text":
            text = request.get("text")

            if not isinstance(text, str):
                return {
                    "ok": False,
                    "error": "text must be a string",
                }

            try:
                channel = int(
                    request.get(
                        "channel",
                        self.config.mesh.channel,
                    )
                )
            except (TypeError, ValueError):
                return {
                    "ok": False,
                    "error": ("channel must be an integer"),
                }

            destination = request.get("destination")

            if destination is not None and not isinstance(
                destination,
                (str, int),
            ):
                return {
                    "ok": False,
                    "error": ("destination must be a string or integer"),
                }

            await self.mesh.send_text(
                text,
                destination,
                channel,
            )

            return {"ok": True}

        if command == "set_provider":
            try:
                self.providers.set_manual(request.get("provider"))
                return {
                    "ok": True,
                    **self.providers.snapshot(),
                }
            except ValueError as exc:
                return {
                    "ok": False,
                    "error": str(exc),
                }

        if command == "reconnect_mesh":
            await self.mesh.reconnect_now()
            return {"ok": True}

        if command == "shutdown":
            self.stop_event.set()
            return {"ok": True}

        if command == "reload_config":
            try:
                return await self.reload_config()
            except Exception as exc:
                log.error(
                    "Configuration reload failed: %s",
                    exc,
                )
                return {
                    "ok": False,
                    "error": str(exc),
                }

        return {
            "ok": False,
            "error": f"unknown command: {command}",
        }

    async def run(self) -> None:
        import time

        self.started_at = time.time()

        await self.router.start()
        await self.ipc.start()

        self.mesh_task = asyncio.create_task(
            self.mesh.run(),
            name="meshtastic-manager",
        )

        loop = asyncio.get_running_loop()

        for sig in (
            signal.SIGINT,
            signal.SIGTERM,
        ):
            try:
                loop.add_signal_handler(
                    sig,
                    self.stop_event.set,
                )
            except NotImplementedError:
                pass

        log.info(
            "Mesh AI Gateway started pid=%s",
            os.getpid(),
        )

        await self.stop_event.wait()

        log.info("Stopping Mesh AI Gateway")

        await self.mesh.stop()

        if self.mesh_task:
            await asyncio.gather(
                self.mesh_task,
                return_exceptions=True,
            )

        await self.router.stop()
        await self.ipc.stop()


def run_daemon(
    config_path: str | Path,
) -> None:
    daemon = GatewayDaemon(config_path)
    asyncio.run(daemon.run())
