from __future__ import annotations

import asyncio
import json
import socket


def request_sync(socket_path: str, payload: dict, timeout: float = 2.0) -> dict:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(timeout)
        sock.connect(socket_path)
        sock.sendall((json.dumps(payload) + "\n").encode("utf-8"))
        chunks = []
        while True:
            data = sock.recv(65536)
            if not data:
                break
            chunks.append(data)
            if b"\n" in data:
                break
    raw = b"".join(chunks).split(b"\n", 1)[0]
    return json.loads(raw.decode("utf-8"))


async def request(socket_path: str, payload: dict, timeout: float = 2.0) -> dict:
    reader, writer = await asyncio.wait_for(
        asyncio.open_unix_connection(socket_path), timeout=timeout
    )
    writer.write((json.dumps(payload) + "\n").encode("utf-8"))
    await writer.drain()
    line = await asyncio.wait_for(reader.readline(), timeout=timeout)
    writer.close()
    await writer.wait_closed()
    return json.loads(line.decode("utf-8"))
