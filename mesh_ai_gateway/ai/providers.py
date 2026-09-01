from __future__ import annotations

import asyncio
import os
import shutil
from pathlib import Path

import httpx

from ..config import ProviderConfig
from ..errors import ProviderError
from .base import AIProvider


class OllamaProvider(AIProvider):
    def __init__(self, config: ProviderConfig):
        super().__init__(config)
        self.client = httpx.AsyncClient(base_url=config.host.rstrip("/") + "/", timeout=None)

    async def generate(self, messages: list[dict[str, str]]) -> str:
        payload = {
            "model": self.config.model,
            "messages": messages,
            "stream": False,
            "options": self.config.options,
        }
        if self.config.keep_alive is not None:
            payload["keep_alive"] = self.config.keep_alive
        try:
            response = await self.client.post("api/chat", json=payload)
            response.raise_for_status()
            data = response.json()
            text = data.get("message", {}).get("content")
            if not isinstance(text, str):
                raise ProviderError("Ollama response did not contain message.content")
            return text.strip()
        except httpx.HTTPStatusError as exc:
            raise ProviderError(
                f"HTTP {exc.response.status_code}: {exc.response.text[:160]}"
            ) from exc
        except httpx.HTTPError as exc:
            raise ProviderError(str(exc)) from exc

    async def healthcheck(self) -> tuple[bool, str]:
        try:
            response = await self.client.get("api/tags", timeout=5.0)
            response.raise_for_status()
            return True, "ready"
        except Exception as exc:
            return False, str(exc)

    async def list_models(self) -> list[str]:
        response = await self.client.get("api/tags", timeout=5.0)
        response.raise_for_status()
        return [
            str(item.get("name")) for item in response.json().get("models", []) if item.get("name")
        ]

    async def aclose(self) -> None:
        await self.client.aclose()


class OpenAICompatibleProvider(AIProvider):
    def __init__(self, config: ProviderConfig):
        super().__init__(config)
        self.client = httpx.AsyncClient(base_url=config.base_url.rstrip("/") + "/", timeout=None)

    def _headers(self) -> dict[str, str]:
        if not self.config.api_key_env:
            return {}
        value = os.environ.get(self.config.api_key_env)
        if not value:
            raise ProviderError(f"environment variable {self.config.api_key_env} is not set")
        return {"Authorization": f"Bearer {value}"}

    async def generate(self, messages: list[dict[str, str]]) -> str:
        payload = {"model": self.config.model, "messages": messages}
        payload.update(self.config.options)
        try:
            response = await self.client.post(
                "chat/completions", json=payload, headers=self._headers()
            )
            response.raise_for_status()
            data = response.json()
            choices = data.get("choices") or []
            text = choices[0].get("message", {}).get("content") if choices else None
            if not isinstance(text, str):
                raise ProviderError(
                    "OpenAI-compatible response did not contain choices[0].message.content"
                )
            return text.strip()
        except httpx.HTTPStatusError as exc:
            raise ProviderError(
                f"HTTP {exc.response.status_code}: {exc.response.text[:160]}"
            ) from exc
        except httpx.HTTPError as exc:
            raise ProviderError(str(exc)) from exc

    async def healthcheck(self) -> tuple[bool, str]:
        try:
            response = await self.client.get("models", headers=self._headers(), timeout=5.0)
            response.raise_for_status()
            return True, "ready"
        except Exception as exc:
            return False, str(exc)

    async def list_models(self) -> list[str]:
        response = await self.client.get("models", headers=self._headers(), timeout=5.0)
        response.raise_for_status()
        return [str(item.get("id")) for item in response.json().get("data", []) if item.get("id")]

    async def aclose(self) -> None:
        await self.client.aclose()


class HermesProvider(AIProvider):
    async def generate(self, messages: list[dict[str, str]]) -> str:
        executable = shutil.which(self.config.command) or self.config.command
        transcript = []
        for message in messages:
            role = message.get("role", "user").upper()
            transcript.append(f"{role}: {message.get('content', '')}")
        prompt = "\n\n".join(transcript)
        args = [executable, "-z", prompt]
        if self.config.model:
            args.extend(["--model", self.config.model])
        proc = None
        try:
            proc = await asyncio.create_subprocess_exec(
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await proc.communicate()
        except asyncio.CancelledError:
            if proc and proc.returncode is None:
                proc.terminate()
                try:
                    await asyncio.wait_for(proc.wait(), timeout=2.0)
                except asyncio.TimeoutError:
                    proc.kill()
                    await proc.wait()
            raise
        except OSError as exc:
            raise ProviderError(str(exc)) from exc
        if proc.returncode != 0:
            detail = stderr.decode("utf-8", "replace").strip()[:240]
            raise ProviderError(detail or f"Hermes exited with code {proc.returncode}")
        return stdout.decode("utf-8", "replace").strip()

    async def healthcheck(self) -> tuple[bool, str]:
        command = Path(self.config.command).expanduser()
        if command.is_absolute() or command.parent != Path("."):
            path = command.resolve()
        else:
            found = shutil.which(self.config.command)
            path = Path(found).resolve() if found else None

        if path is None or not path.is_file() or not os.access(path, os.X_OK):
            return False, f"Hermes executable not found or not executable: {self.config.command}"
        return True, str(path)
