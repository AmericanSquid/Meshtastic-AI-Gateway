from __future__ import annotations

import asyncio
import logging
import socket
import time
from dataclasses import dataclass
from typing import Awaitable, Callable

from ..config import MeshConfig

log = logging.getLogger(__name__)


@dataclass(slots=True)
class MeshMessage:
    text: str
    sender: str
    channel: int
    packet_id: int | None = None
    is_direct: bool = False


class MeshManager:
    def __init__(
        self,
        config: MeshConfig,
        on_message: Callable[
            [MeshMessage],
            Awaitable[None],
        ],
    ):
        self.config = config
        self.on_message = on_message
        self.interface = None
        self.loop: asyncio.AbstractEventLoop | None = None
        self.stop_event = asyncio.Event()
        self.lost_event = asyncio.Event()
        self.force_reconnect_event = asyncio.Event()

        self.detection_sensor_packets: asyncio.Queue[dict] = asyncio.Queue(maxsize=1)

        self._subscribed = False
        self.status = "disconnected"
        self.last_error: str | None = None
        self.attempt = 0
        self.next_retry_at: float | None = None

    def endpoint(self) -> str:
        if self.config.transport == "tcp":
            return f"{self.config.tcp.host}:{self.config.tcp.port}"

        if self.config.transport == "ble":
            return str(self.config.ble.address)

        return str(self.config.serial.port)

    def _subscribe(self) -> None:
        if self._subscribed:
            return

        from pubsub import pub

        pub.subscribe(
            self._on_receive,
            "meshtastic.receive.text",
        )
        pub.subscribe(
            self._on_detection_sensor,
            "meshtastic.receive.detectionsensor",
        )
        pub.subscribe(
            self._on_lost,
            "meshtastic.connection.lost",
        )

        self._subscribed = True

    def _unsubscribe(self) -> None:
        if not self._subscribed:
            return

        try:
            from pubsub import pub

            pub.unsubscribe(
                self._on_receive,
                "meshtastic.receive.text",
            )
            pub.unsubscribe(
                self._on_detection_sensor,
                "meshtastic.receive.detectionsensor",
            )
            pub.unsubscribe(
                self._on_lost,
                "meshtastic.connection.lost",
            )
        except Exception:
            log.debug("Could not unsubscribe Meshtastic callbacks", exc_info=True)

        self._subscribed = False

    def _sender_id(
        self,
        packet: dict,
    ) -> str:
        from_id = packet.get("fromId")

        if from_id:
            return str(from_id)

        from_num = packet.get("from")

        if isinstance(from_num, int):
            return f"!{from_num:08x}"

        return "unknown"

    def _packet_text(
        self,
        packet: dict,
    ) -> str | None:
        decoded = packet.get("decoded") or {}
        text = decoded.get("text")

        if isinstance(text, str):
            return text

        data = decoded.get("data") or {}
        text = data.get("text")

        if isinstance(text, str):
            return text

        payload = data.get("payload")

        if isinstance(payload, bytes):
            try:
                return payload.decode("utf-8")
            except UnicodeDecodeError:
                return None

        return None

    def _on_receive(
        self,
        packet,
        interface=None,
    ):
        text = self._packet_text(packet)

        if not text or not self.loop:
            return

        channel = int(packet.get("channel", 0) or 0)
        if channel != self.config.channel:
            return

        message = MeshMessage(
            text=text,
            sender=self._sender_id(packet),
            channel=channel,
            packet_id=packet.get("id"),
            is_direct=(
                packet.get("to")
                not in (
                    None,
                    0xFFFFFFFF,
                )
            ),
        )

        self.loop.call_soon_threadsafe(lambda: asyncio.create_task(self.on_message(message)))

    def _buffer_detection_sensor(
        self,
        packet: dict,
    ) -> None:
        if self.detection_sensor_packets.full():
            try:
                self.detection_sensor_packets.get_nowait()
            except asyncio.QueueEmpty:
                pass

        self.detection_sensor_packets.put_nowait(packet)

    def _on_detection_sensor(
        self,
        packet,
        interface=None,
    ):
        if not self.loop or interface is not self.interface or not isinstance(packet, dict):
            return

        my_info = getattr(
            interface,
            "myInfo",
            None,
        )
        local_node_num = getattr(
            my_info,
            "my_node_num",
            None,
        )

        if local_node_num is None or packet.get("from") != local_node_num:
            return

        decoded = packet.get("decoded") or {}
        text = decoded.get("text")

        if not isinstance(text, str):
            return

        self.loop.call_soon_threadsafe(
            self._buffer_detection_sensor,
            {
                "from": packet.get("from"),
                "decoded": {
                    "text": text,
                },
            },
        )

    def _on_lost(
        self,
        interface=None,
    ):
        if self.loop:
            self.loop.call_soon_threadsafe(self.lost_event.set)

    def _preflight_tcp(self) -> None:
        with socket.create_connection(
            (
                self.config.tcp.host,
                self.config.tcp.port,
            ),
            timeout=(self.config.reconnect.timeout),
        ):
            return

    def _connect_sync(self):
        timeout = int(
            max(
                1,
                self.config.reconnect.timeout,
            )
        )

        if self.config.transport == "tcp":
            self._preflight_tcp()

            from meshtastic.tcp_interface import (
                TCPInterface,
            )

            return TCPInterface(
                hostname=self.config.tcp.host,
                portNumber=self.config.tcp.port,
                timeout=timeout,
            )

        if self.config.transport == "ble":
            from meshtastic.ble_interface import (
                BLEInterface,
            )

            return BLEInterface(
                address=self.config.ble.address,
                timeout=timeout,
            )

        from meshtastic.serial_interface import (
            SerialInterface,
        )

        return SerialInterface(
            devPath=self.config.serial.port,
            timeout=timeout,
        )

    async def _close_interface(self) -> None:
        interface = self.interface
        self.interface = None

        while not self.detection_sensor_packets.empty():
            try:
                self.detection_sensor_packets.get_nowait()
            except asyncio.QueueEmpty:
                break

        if interface is not None:
            try:
                await asyncio.to_thread(interface.close)
            except Exception as exc:
                log.debug(
                    "Error closing Meshtastic interface: %s",
                    exc,
                    exc_info=True,
                )

    async def connect_cycle(self) -> bool:
        retry = self.config.reconnect

        for attempt in range(
            1,
            retry.attempts + 1,
        ):
            if self.stop_event.is_set():
                return False

            self.attempt = attempt
            self.status = "connecting"
            self.last_error = None
            self.next_retry_at = None

            log.info(
                "Connecting to Meshtastic via %s %s, attempt %s/%s",
                self.config.transport,
                self.endpoint(),
                attempt,
                retry.attempts,
            )

            try:
                self.interface = await asyncio.to_thread(self._connect_sync)
                self.status = "connected"
                self.attempt = 0

                log.info(
                    "Meshtastic connected via %s %s",
                    self.config.transport,
                    self.endpoint(),
                )

                return True
            except Exception as exc:
                self.status = "disconnected"
                self.last_error = str(exc)

                log.warning(
                    "Meshtastic attempt %s/%s failed: %s",
                    attempt,
                    retry.attempts,
                    exc,
                )

                await self._close_interface()

                if attempt < retry.attempts:
                    self.next_retry_at = time.time() + retry.delay

                    try:
                        await asyncio.wait_for(
                            self.stop_event.wait(),
                            timeout=retry.delay,
                        )
                    except asyncio.TimeoutError:
                        pass

        return False

    async def run(self) -> None:
        self.loop = asyncio.get_running_loop()
        self._subscribe()

        try:
            while not self.stop_event.is_set():
                connected = await self.connect_cycle()

                if not connected:
                    self.status = "disconnected"
                    wait = self.config.reconnect.retry_after_failure
                    self.next_retry_at = time.time() + wait

                    log.warning(
                        "Mesh unavailable after %s attempts; retrying in %.0fs",
                        self.config.reconnect.attempts,
                        wait,
                    )

                    try:
                        await asyncio.wait_for(
                            self.force_reconnect_event.wait(),
                            timeout=wait,
                        )
                    except asyncio.TimeoutError:
                        pass

                    self.force_reconnect_event.clear()
                    continue

                self.lost_event.clear()
                self.force_reconnect_event.clear()

                lost_task = asyncio.create_task(self.lost_event.wait())
                force_task = asyncio.create_task(self.force_reconnect_event.wait())
                stop_task = asyncio.create_task(self.stop_event.wait())

                done, pending = await asyncio.wait(
                    {
                        lost_task,
                        force_task,
                        stop_task,
                    },
                    return_when=(asyncio.FIRST_COMPLETED),
                )

                for task in pending:
                    task.cancel()

                if stop_task in done and self.stop_event.is_set():
                    break

                reason = "manual reconnect" if force_task in done else "connection lost"

                self.status = "disconnected"
                self.last_error = reason

                log.warning(
                    "Meshtastic %s; reconnecting",
                    reason,
                )

                await self._close_interface()
        finally:
            await self._close_interface()
            self._unsubscribe()
            self.status = "stopped"

    async def next_detection_sensor_packet(
        self,
        timeout: float,
    ) -> dict:
        return await asyncio.wait_for(
            self.detection_sensor_packets.get(),
            timeout=timeout,
        )

    async def send_text(
        self,
        text: str,
        destination: str | int | None,
        channel: int,
    ) -> None:
        if self.status != "connected" or self.interface is None:
            raise RuntimeError("Meshtastic is not connected")

        kwargs = {
            "channelIndex": channel,
            "wantAck": False,
        }

        if destination is not None:
            kwargs["destinationId"] = destination

        await asyncio.to_thread(
            self.interface.sendText,
            text,
            **kwargs,
        )

    async def reconnect_now(self) -> None:
        self.force_reconnect_event.set()

    async def stop(self) -> None:
        self.stop_event.set()
        self.force_reconnect_event.set()
        self.lost_event.set()

    async def reconfigure(
        self,
        config: MeshConfig,
    ) -> None:
        self.config = config
        await self.reconnect_now()

    def nodes(self) -> list[dict]:
        interface = self.interface

        if interface is None:
            return []

        result = []

        try:
            nodes = (
                getattr(
                    interface,
                    "nodes",
                    {},
                )
                or {}
            )

            for node_id, node in nodes.items():
                user = node.get("user") or {}
                metrics = node.get("deviceMetrics") or {}

                result.append(
                    {
                        "id": node_id,
                        "short": user.get("shortName"),
                        "long": user.get("longName"),
                        "snr": node.get("snr"),
                        "battery": metrics.get("batteryLevel"),
                    }
                )
        except Exception as exc:
            log.debug(
                "Could not snapshot nodes: %s",
                exc,
                exc_info=True,
            )

        return sorted(
            result,
            key=lambda item: item.get("id") or "",
        )

    def snapshot(self) -> dict:
        next_retry = None

        if self.next_retry_at:
            next_retry = max(
                0,
                int(self.next_retry_at - time.time()),
            )

        return {
            "status": self.status,
            "transport": self.config.transport,
            "endpoint": self.endpoint(),
            "attempt": self.attempt,
            "attempts": (self.config.reconnect.attempts),
            "last_error": self.last_error,
            "next_retry_seconds": next_retry,
            "nodes": len(self.nodes()),
        }
