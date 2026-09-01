from __future__ import annotations

import asyncio
import json
import logging
import socket
from pathlib import Path
from typing import Awaitable, Callable

log = logging.getLogger(__name__)


class IPCServer:
    def __init__(
        self,
        socket_path: str,
        handler: Callable[[dict], Awaitable[dict]],
    ):
        self.socket_path = Path(socket_path).expanduser()
        self.handler = handler
        self.server: asyncio.AbstractServer | None = None

    async def start(self) -> None:
        self.socket_path.parent.mkdir(parents=True, exist_ok=True)
        if self.socket_path.exists():
            # Do not unlink a live daemon's control socket. Only remove it when
            # nobody is listening, which means it is stale.
            probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            probe.settimeout(0.5)
            try:
                probe.connect(str(self.socket_path))
            except (ConnectionRefusedError, FileNotFoundError):
                log.info("Removing stale control socket at %s", self.socket_path)
                self.socket_path.unlink(missing_ok=True)
            except OSError as exc:
                raise RuntimeError(
                    f"cannot probe existing control socket {self.socket_path}: {exc}"
                ) from exc
            else:
                raise RuntimeError(
                    f"another gateway daemon is already listening at {self.socket_path}"
                )
            finally:
                probe.close()
        self.server = await asyncio.start_unix_server(self._client, path=str(self.socket_path))
        self.socket_path.chmod(0o600)
        log.info("Control socket listening at %s", self.socket_path)

    async def _client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            line = await asyncio.wait_for(reader.readline(), timeout=5.0)
            if not line:
                writer.close()
                try:
                    await writer.wait_closed()
                except OSError:
                    log.debug("IPC client disconnected while closing connection", exc_info=True)
                return
            request = json.loads(line.decode("utf-8"))
            response = await self.handler(request)
        except Exception as exc:
            log.debug("IPC request failed", exc_info=True)
            response = {"ok": False, "error": str(exc)}
        writer.write((json.dumps(response, ensure_ascii=False) + "\n").encode("utf-8"))
        try:
            await writer.drain()
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except OSError:
                log.debug("IPC client disconnected while closing connection", exc_info=True)

    async def stop(self) -> None:
        if self.server:
            self.server.close()
            await self.server.wait_closed()
            self.server = None
        self.socket_path.unlink(missing_ok=True)
