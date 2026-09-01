from __future__ import annotations

import importlib.util
import os
import queue
import sys
import threading
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType

from ..ipc.client import request as gateway_request


_RUNTIME_MODULE_NAME = "mesh_ai_gateway._laundry_hmm_runtime"
_STOP_MODULE = object()


def _laundry_source_path() -> Path:
    configured = os.environ.get("LAUNDRY_HMM_SOURCE")
    candidates = [
        Path(configured).expanduser() if configured else None,
        Path("/opt/mesh-ai-gateway/mesh_ai_gateway/tui/modules/laundry_hmm.py"),
        Path(__file__).with_name("modules") / "laundry_hmm.py",
    ]
    for candidate in candidates:
        if candidate is not None and candidate.is_file():
            return candidate
    return candidates[-1]


@dataclass(frozen=True)
class ModuleSpec:
    module_id: str
    name: str
    command: tuple[str, ...]
    cwd: Path


class _StopModule(BaseException):
    pass


class _LineBuffer:
    def __init__(self, max_lines: int) -> None:
        self.lines: deque[str] = deque(maxlen=max_lines)
        self.current_line = ""
        self.lock = threading.Lock()

    def clear(self) -> None:
        with self.lock:
            self.lines.clear()
            self.current_line = ""

    def write(self, text: str) -> None:
        text = str(text).replace("\r\n", "\n").replace("\r", "\n")
        pieces = text.split("\n")
        with self.lock:
            self.current_line += pieces[0]
            for piece in pieces[1:]:
                self.lines.append(self.current_line)
                self.current_line = piece

    def snapshot(self, limit: int, suffix: str = "") -> list[str]:
        if limit <= 0:
            return []
        with self.lock:
            output = list(self.lines)
            current = self.current_line + suffix
            if current:
                output.append(current)
        return output[-limit:]


class LaundryModule:
    """Run laundry_hmm.py inside the TUI process, never as a subprocess."""

    def __init__(self, socket_path: str, max_lines: int = 5000) -> None:
        self.socket_path = socket_path
        self.source_path = _laundry_source_path()
        self.spec = ModuleSpec(
            module_id="laundry",
            name="Laundry HMM",
            command=("in-process", str(self.source_path)),
            cwd=self.source_path.parent,
        )
        self.output = _LineBuffer(max_lines)
        self.inputs: queue.Queue[object] = queue.Queue()
        self.input_buffer = ""
        self.thread: threading.Thread | None = None
        self.runtime: ModuleType | None = None
        self.exit_code: int | None = None
        self.stopping = False

    @property
    def running(self) -> bool:
        return self.thread is not None and self.thread.is_alive()

    @property
    def status(self) -> str:
        if self.running:
            return "STOPPING" if self.stopping else "RUNNING"
        if self.exit_code is None:
            return "STOPPED"
        return f"EXIT {self.exit_code}"

    def _print(
        self,
        *values,
        sep: str = " ",
        end: str = "\n",
        file=None,
        flush: bool = False,
    ) -> None:
        del file, flush
        self.output.write(sep.join(str(value) for value in values) + end)

    def _input(self, prompt: str = "") -> str:
        if prompt:
            self.output.write(prompt)
        value = self.inputs.get()
        if value is _STOP_MODULE:
            raise _StopModule
        return str(value)

    def _load_runtime(self) -> ModuleType:
        if not self.source_path.is_file():
            raise FileNotFoundError(f"Laundry module not found: {self.source_path}")

        spec = importlib.util.spec_from_file_location(
            _RUNTIME_MODULE_NAME,
            self.source_path,
        )
        if spec is None or spec.loader is None:
            raise RuntimeError(f"Could not load Laundry module: {self.source_path}")

        module = importlib.util.module_from_spec(spec)
        module.print = self._print
        module.input = self._input
        module.request = gateway_request
        sys.modules[_RUNTIME_MODULE_NAME] = module

        try:
            spec.loader.exec_module(module)
        except BaseException:
            sys.modules.pop(_RUNTIME_MODULE_NAME, None)
            raise

        module.GATEWAY_SOCKET_PATH = self.socket_path
        return module

    def _run(self) -> None:
        try:
            self.runtime = self._load_runtime()
            self.runtime.menu()
            self.exit_code = 0
        except _StopModule:
            self.exit_code = 0
        except SystemExit as exc:
            self.exit_code = int(exc.code) if isinstance(exc.code, int) else 1
        except BaseException as exc:
            self._print(f"\nLaundry module error: {exc}")
            self.exit_code = 1
        finally:
            self.runtime = None
            self.stopping = False

    def start(self) -> None:
        if self.running:
            return

        self.output.clear()
        self.input_buffer = ""
        self.exit_code = None
        self.stopping = False

        while True:
            try:
                self.inputs.get_nowait()
            except queue.Empty:
                break

        self.thread = threading.Thread(
            target=self._run,
            name="laundry-hmm",
            daemon=True,
        )
        self.thread.start()

    def poll(self) -> None:
        thread = self.thread
        if thread is not None and not thread.is_alive():
            thread.join(timeout=0)
            self.thread = None

    def send(self, data: bytes) -> None:
        if not self.running:
            return

        for value in data:
            if value == 3:
                self.interrupt()
            elif value in (10, 13):
                submitted = self.input_buffer
                self.output.write(submitted + "\n")
                self.input_buffer = ""
                self.inputs.put(submitted)
            elif value in (8, 127):
                self.input_buffer = self.input_buffer[:-1]
            elif 32 <= value <= 126:
                self.input_buffer += chr(value)

    def interrupt(self) -> None:
        runtime = self.runtime
        if runtime is not None and hasattr(runtime, "request_stop"):
            runtime.request_stop()

    def terminate(self) -> None:
        if not self.running:
            return

        self.stopping = True
        self.interrupt()
        self.inputs.put(_STOP_MODULE)

    def resize(self, rows: int, cols: int) -> None:
        del rows, cols

    def display_lines(self, limit: int) -> list[str]:
        self.poll()
        return self.output.snapshot(limit, self.input_buffer)


class ModuleManager:
    def __init__(self, socket_path: str) -> None:
        laundry = LaundryModule(socket_path)
        self.modules = {laundry.spec.module_id: laundry}
        self.order = [laundry.spec.module_id]

    def poll(self) -> None:
        for module in self.modules.values():
            module.poll()

    def get(self, module_id: str) -> LaundryModule:
        return self.modules[module_id]

    def by_index(self, index: int) -> LaundryModule | None:
        if index < 0 or index >= len(self.order):
            return None
        return self.modules[self.order[index]]

    def running(self) -> list[LaundryModule]:
        self.poll()
        return [module for module in self.modules.values() if module.running]
