from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import signal
from pathlib import Path

import httpx

from ..config import ProviderConfig
from ..errors import ProviderError
from .base import AIProvider


log = logging.getLogger(__name__)


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
            raise ProviderError(f"HTTP {exc.response.status_code}: {exc.response.text[:160]}") from exc
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
        return [str(item.get("name")) for item in response.json().get("models", []) if item.get("name")]


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
                raise ProviderError("OpenAI-compatible response did not contain choices[0].message.content")
            return text.strip()
        except httpx.HTTPStatusError as exc:
            raise ProviderError(f"HTTP {exc.response.status_code}: {exc.response.text[:160]}") from exc
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


class HermesProvider(AIProvider):
    def __init__(self, config: ProviderConfig):
        super().__init__(config)
        self.proc: asyncio.subprocess.Process | None = None
        self._lock = asyncio.Lock()
        self._stderr_task: asyncio.Task | None = None

    @property
    def running(self) -> bool:
        return self.proc is not None and self.proc.returncode is None

    def _command_path(self) -> Path:
        executable = shutil.which(self.config.command) or self.config.command
        path = Path(executable).expanduser().resolve()
        if not path.is_file():
            raise ProviderError(f"Hermes executable not found: {self.config.command}")
        return path

    def _python_path(self, executable: Path) -> str:
        for name in ("python", "python3"):
            candidate = executable.parent / name
            if candidate.is_file():
                return str(candidate)

        try:
            first_line = executable.open("rb").readline().decode("utf-8", "replace").strip()
        except OSError:
            first_line = ""
        if first_line.startswith("#!"):
            interpreter = first_line[2:].strip().split()[0]
            if interpreter and Path(interpreter).is_file() and "python" in Path(interpreter).name:
                return interpreter

        raise ProviderError(f"Could not locate Hermes Python interpreter beside {executable}")

    async def _drain_stderr(self, stream: asyncio.StreamReader) -> None:
        while True:
            line = await stream.readline()
            if not line:
                return
            text = line.decode("utf-8", "replace").rstrip()
            if text:
                log.debug("Hermes: %s", text)

    async def _start_locked(self) -> None:
        if self.running:
            return

        await self._stop_locked(force=True)

        executable = self._command_path()
        python = self._python_path(executable)
        worker = Path(__file__).resolve().parent.parent / "hermes_worker.py"
        args = [python, "-u", str(worker)]
        if self.config.model:
            args.extend(["--model", self.config.model])

        env = os.environ.copy()
        env.setdefault("HERMES_YOLO_MODE", "1")
        env.setdefault("HERMES_ACCEPT_HOOKS", "1")
        env["PYTHONUNBUFFERED"] = "1"

        try:
            self.proc = await asyncio.create_subprocess_exec(
                *args,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(Path.home()),
                env=env,
                start_new_session=True,
            )
        except OSError as exc:
            self.proc = None
            raise ProviderError(str(exc)) from exc

        assert self.proc.stderr is not None
        self._stderr_task = asyncio.create_task(
            self._drain_stderr(self.proc.stderr),
            name="hermes-worker-stderr",
        )

        startup_timeout = float(self.config.options.get("startup_timeout", 120.0))
        try:
            response = await asyncio.wait_for(
                self._read_response_locked(),
                timeout=startup_timeout,
            )
        except Exception:
            await self._stop_locked(force=True)
            raise

        if not response.get("ok"):
            error = str(response.get("error") or "Hermes worker failed to start")
            await self._stop_locked(force=True)
            raise ProviderError(error)

        log.info(
            "Hermes agent ready provider=%s model=%s",
            response.get("provider") or "unknown",
            response.get("model") or self.config.model or "default",
        )

    async def start(self) -> None:
        async with self._lock:
            await self._start_locked()

    async def _read_response_locked(self) -> dict:
        if not self.running or self.proc is None or self.proc.stdout is None:
            raise ProviderError("Hermes agent is not running")
        line = await self.proc.stdout.readline()
        if not line:
            code = self.proc.returncode
            raise ProviderError(f"Hermes worker exited unexpectedly ({code})")
        try:
            response = json.loads(line.decode("utf-8", "replace"))
        except json.JSONDecodeError as exc:
            raise ProviderError(f"Invalid response from Hermes worker: {line[:160]!r}") from exc
        if not isinstance(response, dict):
            raise ProviderError("Invalid response from Hermes worker")
        return response

    async def _request_locked(self, payload: dict) -> dict:
        if not self.running or self.proc is None or self.proc.stdin is None:
            raise ProviderError("Hermes agent is not running")
        self.proc.stdin.write((json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8"))
        await self.proc.stdin.drain()
        response = await self._read_response_locked()
        if not response.get("ok"):
            raise ProviderError(str(response.get("error") or "Hermes request failed"))
        return response

    async def generate(self, messages: list[dict[str, str]]) -> str:
        prompt = next(
            (message.get("content", "") for message in reversed(messages) if message.get("role") == "user"),
            "",
        )
        system_prompt = next(
            (message.get("content", "") for message in messages if message.get("role") == "system"),
            "",
        )
        timeout = float(self.config.options.get("timeout", 180.0))

        async with self._lock:
            await self._start_locked()
            try:
                response = await asyncio.wait_for(
                    self._request_locked(
                        {
                            "op": "generate",
                            "prompt": prompt,
                            "system_prompt": system_prompt,
                        }
                    ),
                    timeout=timeout,
                )
            except (asyncio.TimeoutError, asyncio.CancelledError):
                await self._stop_locked(force=True)
                raise

        return str(response.get("response") or "").strip()

    async def reset(self) -> None:
        async with self._lock:
            if not self.running:
                return
            await self._request_locked({"op": "reset"})

    async def _stop_locked(self, force: bool = False) -> None:
        proc, self.proc = self.proc, None
        stderr_task, self._stderr_task = self._stderr_task, None

        if proc is not None and proc.returncode is None:
            if not force and proc.stdin is not None:
                try:
                    proc.stdin.write(b'{"op":"stop"}\n')
                    await proc.stdin.drain()
                    await asyncio.wait_for(proc.wait(), timeout=5.0)
                except Exception:
                    force = True

            if force and proc.returncode is None:
                try:
                    os.killpg(proc.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                try:
                    await asyncio.wait_for(proc.wait(), timeout=5.0)
                except asyncio.TimeoutError:
                    try:
                        os.killpg(proc.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    await proc.wait()

        if stderr_task is not None:
            stderr_task.cancel()
            await asyncio.gather(stderr_task, return_exceptions=True)

    async def stop(self) -> None:
        async with self._lock:
            await self._stop_locked()

    async def healthcheck(self) -> tuple[bool, str]:
        if self.running:
            return True, "running"
        try:
            path = self._command_path()
            self._python_path(path)
            return True, str(path)
        except Exception as exc:
            return False, str(exc)
