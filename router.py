from __future__ import annotations

import asyncio
import logging

from .ai.manager import ProviderManager
from .ai.providers import HermesProvider
from .config import BridgeConfig
from .errors import ProviderExhausted
from .mesh.manager import MeshManager, MeshMessage
from .mesh_commands import handle_mesh_command
from .sessions import SessionStore
from .util import split_utf8, truncate_utf8

log = logging.getLogger(__name__)


class MessageRouter:
    def __init__(
        self,
        mesh: MeshManager,
        providers: ProviderManager,
        bridge_config: BridgeConfig,
    ):
        self.mesh = mesh

        self.providers = providers
        self.config = bridge_config

        self.hermes = None
        self.hermes_selected = False

        for provider in self.providers.config.providers.values():
            if provider.enabled and provider.type == "hermes":
                self.hermes = HermesProvider(provider)
                break

        self.sessions = SessionStore(bridge_config.history_messages)
        self.queue: asyncio.Queue[MeshMessage] = asyncio.Queue(maxsize=bridge_config.queue_size)
        self.workers: list[asyncio.Task] = []
        self.stopped = False

    async def enqueue(self, message: MeshMessage) -> None:
        try:
            self.queue.put_nowait(message)
            log.info("RX sender=%s channel=%s text=%r", message.sender, message.channel, message.text)
        except asyncio.QueueFull:
            log.warning("Incoming queue full; dropping message from %s", message.sender)
            try:
                await self.mesh.send_text(
                    "AI gateway busy. Try again shortly.",
                    self._reply_destination(message),
                    message.channel,
                )
            except Exception:
                pass

    async def start(self) -> None:
        self.stopped = False
        self.workers = [
            asyncio.create_task(self._worker(index), name=f"message-worker-{index}")
            for index in range(self.config.max_concurrent_requests)
        ]

    async def stop(self) -> None:
        self.stopped = True
        for task in self.workers:
            task.cancel()
        await asyncio.gather(*self.workers, return_exceptions=True)
        self.workers.clear()
        if self.hermes is not None:
            await self.hermes.stop()

    async def reconfigure(self, config: BridgeConfig) -> None:
        workers_changed = config.max_concurrent_requests != self.config.max_concurrent_requests
        self.config = config
        self.sessions.reconfigure(config.history_messages)

        hermes_config = next(
            (
                provider
                for provider in self.providers.config.providers.values()
                if provider.enabled and provider.type == "hermes"
            ),
            None,
        )
        if hermes_config is None:
            if self.hermes is not None:
                await self.hermes.stop()
            self.hermes = None
            self.hermes_selected = False
        elif self.hermes is None or self.hermes.config != hermes_config:
            if self.hermes is not None:
                await self.hermes.stop()
            self.hermes = HermesProvider(hermes_config)
            self.hermes_selected = False

        if workers_changed:
            await self.stop()
            await self.start()

    def _session_key(self, message: MeshMessage) -> str:
        return message.sender

    def _reply_destination(self, message: MeshMessage) -> str | int:
        return message.sender if message.is_direct else 0xFFFFFFFF

    async def _command(self, message: MeshMessage) -> str | tuple[str, ...] | None:
        response = await handle_mesh_command(self, message)
        if response is not None:
            return response

        raw = message.text.strip()
        if not raw.startswith("!"):
            return None
        parts = raw.split()
        command = parts[0].lower()
        key = self._session_key(message)
        if command == "!reset":
            self.sessions.reset(key)
            return "Conversation reset."
        if command == "!status":
            mesh = self.mesh.snapshot()
            ai = self.providers.snapshot()
            active = ai.get("active") or ai.get("selected") or "none"
            return f"Mesh: {mesh['status']} ({mesh['transport']}). AI: {active}."
        if command == "!provider":
            if len(parts) == 1:
                snapshot = self.providers.snapshot()
                choices = [item["id"] for item in snapshot["providers"]]

                if self.hermes is not None:
                    choices.append("hermes")

                selected = "hermes" if self.hermes_selected else snapshot["selected"]

                return f"Provider: {selected}. Available: auto, {', '.join(choices)}"

            target = parts[1]

            if target == "hermes":
                if self.hermes is None:
                    return "Hermes is not enabled."

                self.hermes_selected = True
                self.providers.set_manual(None)
                return "Hermes agent selected."

            self.hermes_selected = False

            try:
                self.providers.set_manual(target)
                return f"Provider set to {target}."
            except ValueError as exc:
                return str(exc)
        return None

    async def _send_response(self, message: MeshMessage, response: str) -> None:
        response = truncate_utf8(response, self.config.response_max_bytes)
        chunks = split_utf8(response, self.config.chunk_bytes)
        for index, chunk in enumerate(chunks):
            destination = self._reply_destination(message)
            await self.mesh.send_text(chunk, destination, message.channel)
            log.info("TX recipient=%s channel=%s text=%r", destination, message.channel, chunk)
            if index + 1 < len(chunks) and self.config.chunk_delay:
                await asyncio.sleep(self.config.chunk_delay)

    async def _handle(self, message: MeshMessage) -> None:
        command_response = await self._command(message)
        if command_response is not None:
            if isinstance(command_response, (list, tuple)):
                for index, response in enumerate(command_response):
                    await self._send_response(message, response)
                    if index + 1 < len(command_response):
                        await asyncio.sleep(4.0)
            else:
                await self._send_response(message, command_response)
            return

        key = self._session_key(message)
        history = self.sessions.get(key)
        messages = [{"role": "system", "content": self.config.system_prompt}]
        messages.extend(history)
        messages.append({"role": "user", "content": message.text})

        try:
            if self.hermes_selected and self.hermes is not None:
                response = await self.hermes.generate(messages)
                provider_id = "hermes"
            else:
                response, provider_id = await self.providers.generate(messages)
            self.sessions.append(key, "user", message.text)
            self.sessions.append(key, "assistant", response)
            log.info("AI response provider=%s sender=%s", provider_id, message.sender)
        except ProviderExhausted as exc:
            log.error("All AI providers failed for %s: %s", message.sender, exc)
            response = "AI providers unavailable right now."
        except Exception as exc:
            if not self.hermes_selected:
                raise
            log.exception("Hermes failed for %s", message.sender)
            response = f"Hermes error: {type(exc).__name__}: {str(exc)[:120]}"

        await self._send_response(message, response)

    async def _worker(self, index: int) -> None:
        while True:
            message = await self.queue.get()
            try:
                await self._handle(message)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.exception("Message handling failed for %s: %s", message.sender, exc)
                try:
                    await self.mesh.send_text(
                        "Gateway error while handling that message.",
                        self._reply_destination(message),
                        message.channel,
                    )
                except Exception:
                    pass
            finally:
                self.queue.task_done()
