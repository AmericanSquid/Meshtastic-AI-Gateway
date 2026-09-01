from __future__ import annotations

import codecs
import errno
import fcntl
import os
import pty
import re
import shutil
import signal
import struct
import termios
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping, Sequence


_ANSI_RE = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))")


@dataclass(frozen=True)
class ModuleSpec:
    module_id: str
    name: str
    command: Sequence[str]
    cwd: Path | None = None
    env: Mapping[str, str] = field(default_factory=dict)


class TerminalModule:
    """Run one ordinary terminal program inside a PTY owned by the TUI."""

    def __init__(self, spec: ModuleSpec, max_lines: int = 5000) -> None:
        self.spec = spec
        self.max_lines = max_lines
        self.pid: int | None = None
        self.master_fd: int | None = None
        self.exit_code: int | None = None
        self.lines: deque[str] = deque(maxlen=max_lines)
        self.current_line = ""
        self.decoder = codecs.getincrementaldecoder("utf-8")("replace")

    @property
    def running(self) -> bool:
        return self.pid is not None and self.exit_code is None

    @property
    def status(self) -> str:
        if self.running:
            return "RUNNING"
        if self.exit_code is None:
            return "STOPPED"
        return f"EXIT {self.exit_code}"

    def _resolved_command(self) -> list[str]:
        command = [str(part) for part in self.spec.command]
        if not command:
            raise ValueError(f"Module {self.spec.module_id!r} has an empty command")

        executable = command[0]
        if os.path.sep not in executable:
            resolved = shutil.which(executable)
            if resolved is None:
                raise FileNotFoundError(f"Executable not found: {executable}")
            command[0] = resolved
        elif not Path(executable).expanduser().exists():
            raise FileNotFoundError(f"Executable not found: {executable}")

        return command

    def start(self) -> None:
        if self.running:
            return

        command = self._resolved_command()
        cwd = self.spec.cwd.expanduser() if self.spec.cwd is not None else None
        env = os.environ.copy()
        env.update({str(key): str(value) for key, value in self.spec.env.items()})

        self.exit_code = None
        self.lines.clear()
        self.current_line = ""
        self.decoder = codecs.getincrementaldecoder("utf-8")("replace")

        pid, master_fd = pty.fork()
        if pid == 0:
            try:
                if cwd is not None:
                    os.chdir(cwd)
                os.execvpe(command[0], command, env)
            except BaseException as exc:
                try:
                    print(f"Could not start module: {exc}", flush=True)
                finally:
                    os._exit(127)

        self.pid = pid
        self.master_fd = master_fd
        os.set_blocking(master_fd, False)

    def _append_output(self, data: bytes) -> None:
        text = self.decoder.decode(data)
        if not text:
            return

        text = _ANSI_RE.sub("", text)
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        pieces = text.split("\n")

        self.current_line += pieces[0]
        for piece in pieces[1:]:
            self.lines.append(self.current_line)
            self.current_line = piece

    def _finish_process(self, wait_status: int) -> None:
        if os.WIFEXITED(wait_status):
            self.exit_code = os.WEXITSTATUS(wait_status)
        elif os.WIFSIGNALED(wait_status):
            self.exit_code = -os.WTERMSIG(wait_status)
        else:
            self.exit_code = 1

        self.pid = None

    def poll(self) -> None:
        fd = self.master_fd
        if fd is not None:
            while True:
                try:
                    data = os.read(fd, 8192)
                    if not data:
                        break
                    self._append_output(data)
                except BlockingIOError:
                    break
                except OSError as exc:
                    if exc.errno == errno.EIO:
                        break
                    raise

        if self.pid is not None:
            try:
                waited_pid, status = os.waitpid(self.pid, os.WNOHANG)
            except ChildProcessError:
                waited_pid, status = self.pid, 0
            if waited_pid:
                self._finish_process(status)

        if self.pid is None and self.master_fd is not None:
            try:
                while True:
                    data = os.read(self.master_fd, 8192)
                    if not data:
                        break
                    self._append_output(data)
            except (BlockingIOError, OSError):
                pass
            os.close(self.master_fd)
            self.master_fd = None

    def send(self, data: bytes) -> None:
        if not self.running or self.master_fd is None:
            return
        os.write(self.master_fd, data)

    def resize(self, rows: int, cols: int) -> None:
        if self.master_fd is None:
            return
        rows = max(1, rows)
        cols = max(1, cols)
        try:
            fcntl.ioctl(
                self.master_fd,
                termios.TIOCSWINSZ,
                struct.pack("HHHH", rows, cols, 0, 0),
            )
        except OSError:
            pass

    def display_lines(self, limit: int) -> list[str]:
        self.poll()
        output = list(self.lines)
        if self.current_line:
            output.append(self.current_line)
        if limit <= 0:
            return []
        return output[-limit:]

    def terminate(self) -> None:
        if self.pid is None:
            return

        pid = self.pid
        try:
            os.killpg(pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass

    def kill(self) -> None:
        if self.pid is None:
            return

        pid = self.pid
        try:
            os.killpg(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


class ModuleManager:
    def __init__(self, specs: Sequence[ModuleSpec]) -> None:
        self.modules = {
            spec.module_id: TerminalModule(spec)
            for spec in specs
        }
        self.order = [spec.module_id for spec in specs]

    def poll(self) -> None:
        for module in self.modules.values():
            module.poll()

    def get(self, module_id: str) -> TerminalModule:
        return self.modules[module_id]

    def by_index(self, index: int) -> TerminalModule | None:
        if index < 0 or index >= len(self.order):
            return None
        return self.modules[self.order[index]]

    def running(self) -> list[TerminalModule]:
        self.poll()
        return [module for module in self.modules.values() if module.running]
