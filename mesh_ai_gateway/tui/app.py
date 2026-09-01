from __future__ import annotations

import curses
import os
import subprocess
import time
from pathlib import Path

from ..ipc.client import request_sync
from ..service.systemd import action as service_action
from ..service.systemd import status as service_status
from .modules import ModuleManager


class TUI:
    def __init__(self, stdscr, socket_path: str, config_path: Path):
        self.stdscr = stdscr
        self.socket_path = socket_path
        self.config_path = config_path
        self.screen = "dashboard"
        self.message = ""
        self.last_status: dict | None = None

        self.modules = ModuleManager(socket_path)
        self.active_module_id: str | None = None
        self.last_module_id: str | None = None

    def ipc(self, command: str, timeout: float = 2.0, **kwargs) -> dict:
        try:
            return request_sync(
                self.socket_path,
                {"command": command, **kwargs},
                timeout=timeout,
            )
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def draw_line(self, row: int, text: str) -> None:
        height, width = self.stdscr.getmaxyx()
        if row >= height:
            return
        try:
            self.stdscr.addnstr(
                row,
                0,
                text.ljust(max(1, width - 1)),
                max(1, width - 1),
            )
        except curses.error:
            pass

    def dashboard(self) -> None:
        daemon = self.ipc("status")
        service = service_status()
        self.last_status = daemon if daemon.get("ok") else None

        self.draw_line(0, "MESH AI GATEWAY")
        self.draw_line(1, "=" * 72)

        if daemon.get("ok"):
            mesh = daemon["mesh"]
            ai = daemon["ai"]
            active_id = ai.get("active")

            if active_id:
                active_name = next(
                    (
                        provider["name"]
                        for provider in ai["providers"]
                        if provider["id"] == active_id
                    ),
                    active_id,
                )
            else:
                active_name = "none yet"

            self.draw_line(
                2,
                f"Daemon : RUNNING  pid={daemon['pid']}  uptime={daemon['uptime_seconds']}s",
            )

            mesh_detail = f"{mesh['transport']} {mesh['endpoint']}"
            self.draw_line(
                3,
                f"Mesh   : {mesh['status'].upper():12} {mesh_detail}",
            )

            if mesh.get("last_error"):
                retry = mesh.get("next_retry_seconds")
                retry_text = f"; retry in {retry}s" if retry is not None else ""
                self.draw_line(
                    4,
                    f"         last error: {mesh['last_error']}{retry_text}",
                )
            else:
                self.draw_line(
                    4,
                    f"         nodes: {mesh.get('nodes', 0)}",
                )

            self.draw_line(
                5,
                f"Routing Mode     : {ai['mode'].upper()}",
            )
            self.draw_line(
                6,
                f"Active Provider  : {active_name}",
            )

            providers = " -> ".join(provider["name"] for provider in ai["providers"])
            self.draw_line(
                7,
                f"Routing Priority : {providers}",
            )

            if ai.get("hermes_enabled"):
                hermes_status = "SELECTED" if ai.get("hermes_selected") else "ENABLED"
            else:
                hermes_status = "DISABLED"

            self.draw_line(7, f"Hermes : {hermes_status}")
            self.draw_line(
                8,
                f"Queue  : {daemon['queue']['size']}/{daemon['queue']['capacity']}",
            )
        else:
            self.draw_line(
                2,
                f"Daemon : STOPPED/UNREACHABLE  systemd={service['active']}",
            )
            self.draw_line(3, f"Socket : {self.socket_path}")
            self.draw_line(
                4,
                f"Reason : {daemon.get('error', 'not running')}",
            )
            self.draw_line(
                5,
                f"Autostart: {service['enabled']}",
            )

        running_modules = len(self.modules.running())

        self.draw_line(
            9,
            "[S] Start/Stop Daemon  [R] Restart  [M] Reconnect Mesh  [P] Providers",
        )
        self.draw_line(
            10,
            f"[N] Nodes  [L] Logs  [C] Edit YAML  "
            f"[U] Modules ({running_modules} running)  "
            "[Q] Detach TUI",
        )

        if self.message:
            self.draw_line(12, self.message)

    def logs(self) -> None:
        response = self.ipc(
            "logs",
            limit=max(
                20,
                self.stdscr.getmaxyx()[0] - 4,
            ),
        )

        self.draw_line(0, "LOGS  [Esc/L] back")
        self.draw_line(1, "=" * 72)

        lines = (
            response.get("lines", [])
            if response.get("ok")
            else [
                response.get(
                    "error",
                    "daemon unavailable",
                )
            ]
        )

        available_rows = self.stdscr.getmaxyx()[0] - 3

        for index, line in enumerate(
            lines[-available_rows:],
            start=2,
        ):
            self.draw_line(index, line)

    def nodes(self) -> None:
        response = self.ipc("nodes")

        self.draw_line(0, "NODES  [Esc/N] back")
        self.draw_line(1, "=" * 72)

        nodes = response.get("nodes", []) if response.get("ok") else []

        if not response.get("ok"):
            self.draw_line(
                2,
                response.get(
                    "error",
                    "daemon unavailable",
                ),
            )
            return

        available_rows = self.stdscr.getmaxyx()[0] - 3

        for row, node in enumerate(
            nodes[:available_rows],
            start=2,
        ):
            label = node.get("long") or node.get("short") or "?"
            self.draw_line(
                row,
                f"{node.get('id', '?'):12} "
                f"{label:24} "
                f"SNR={node.get('snr')} "
                f"battery={node.get('battery')}",
            )

    def providers(self) -> None:
        response = self.ipc("providers")

        self.draw_line(
            0,
            "PROVIDERS  [0] Auto  [1-9] Select  [T] Test  [Esc/P] back",
        )
        self.draw_line(1, "=" * 72)

        if not response.get("ok"):
            self.draw_line(
                2,
                response.get(
                    "error",
                    "daemon unavailable",
                ),
            )
            return

        self._provider_snapshot = response

        for index, provider in enumerate(
            response["providers"][:9],
            start=1,
        ):
            marker = "*" if response.get("active") == provider["id"] else " "
            detail = provider.get("last_error") or provider.get("status")

            self.draw_line(
                index + 1,
                f"[{index}] {marker} "
                f"{provider['name']:24} "
                f"{provider['type']:8} "
                f"{provider.get('model') or '-':20} "
                f"{detail}",
            )

    def module_menu(self) -> None:
        self.modules.poll()

        self.draw_line(
            0,
            "MODULES  [1-9] Open/Start  [K] Stop last module  [Esc/U] back",
        )
        self.draw_line(1, "=" * 72)

        if not self.modules.order:
            self.draw_line(
                2,
                "No modules configured.",
            )
            return

        for index, module_id in enumerate(
            self.modules.order[:9],
            start=1,
        ):
            module = self.modules.get(module_id)
            marker = "*" if module_id == self.last_module_id else " "
            command = " ".join(str(part) for part in module.spec.command)

            self.draw_line(
                index + 1,
                f"[{index}] {marker} {module.spec.name:24} {module.status:12} {command}",
            )

        if self.message:
            row = min(
                self.stdscr.getmaxyx()[0] - 1,
                len(self.modules.order) + 3,
            )
            self.draw_line(row, self.message)

    def module_terminal(self) -> None:
        if self.active_module_id is None:
            self.screen = "modules"
            return

        module = self.modules.get(self.active_module_id)
        module.poll()

        height, width = self.stdscr.getmaxyx()

        module.resize(
            max(1, height - 2),
            max(1, width - 1),
        )

        self.draw_line(
            0,
            f"{module.spec.name}  [{module.status}]  [F10] Back  [Ctrl+C/I] Interrupt",
        )
        self.draw_line(1, "=" * 72)

        output_rows = max(0, height - 2)

        for row, line in enumerate(
            module.display_lines(output_rows),
            start=2,
        ):
            if row >= height:
                break
            self.draw_line(row, line)

    def edit_config(self) -> None:
        editor = os.environ.get(
            "EDITOR",
            "vi",
        )

        curses.def_prog_mode()
        curses.endwin()

        try:
            subprocess.run(
                [editor, str(self.config_path)],
                check=False,
            )
        finally:
            curses.reset_prog_mode()
            self.stdscr.refresh()

        result = self.ipc("reload_config")

        self.message = (
            "Config reloaded." if result.get("ok") else f"Config error: {result.get('error')}"
        )

    def toggle_service(self) -> None:
        state = service_status()
        daemon = self.ipc("status")

        if state["running"]:
            ok, detail = service_action("stop")
            self.message = (f"systemd stop: {'ok' if ok else 'failed'} {detail}").strip()
        elif daemon.get("ok"):
            result = self.ipc("shutdown")
            self.message = (
                "Manual daemon stop requested."
                if result.get("ok")
                else result.get(
                    "error",
                    "stop failed",
                )
            )
        else:
            ok, detail = service_action("start")
            self.message = (f"systemd start: {'ok' if ok else 'failed'} {detail}").strip()

        time.sleep(0.4)

    def open_module(self, index: int) -> None:
        module = self.modules.by_index(index)

        if module is None:
            return

        try:
            if not module.running:
                module.start()

            self.active_module_id = module.spec.module_id
            self.last_module_id = module.spec.module_id
            self.screen = "module_terminal"
            self.message = ""
        except Exception as exc:
            self.message = f"Could not start {module.spec.name}: {exc}"

    def _module_key_bytes(
        self,
        key: int,
    ) -> bytes | None:
        if key in (
            10,
            13,
            curses.KEY_ENTER,
        ):
            return b"\r"

        if key in (
            curses.KEY_BACKSPACE,
            127,
            8,
        ):
            return b"\x7f"

        if key == curses.KEY_UP:
            return b"\x1b[A"

        if key == curses.KEY_DOWN:
            return b"\x1b[B"

        if key == curses.KEY_RIGHT:
            return b"\x1b[C"

        if key == curses.KEY_LEFT:
            return b"\x1b[D"

        if key == curses.KEY_HOME:
            return b"\x1b[H"

        if key == curses.KEY_END:
            return b"\x1b[F"

        if key == curses.KEY_DC:
            return b"\x1b[3~"

        if key == curses.KEY_PPAGE:
            return b"\x1b[5~"

        if key == curses.KEY_NPAGE:
            return b"\x1b[6~"

        if 0 <= key <= 255:
            return bytes((key,))

        return None

    def handle_module_terminal_key(
        self,
        key: int,
    ) -> bool:
        if key == curses.KEY_F10:
            self.screen = "modules"
            self.active_module_id = None
            return True

        if key == curses.KEY_RESIZE:
            return True

        if self.active_module_id is None:
            self.screen = "modules"
            return True

        module = self.modules.get(self.active_module_id)

        if key in (
            ord("i"),
            ord("I"),
        ):
            module.interrupt()
            return True

        payload = self._module_key_bytes(key)

        if payload is not None:
            module.send(payload)

        return True

    def handle_key(self, key: int) -> bool:
        if self.screen == "module_terminal":
            return self.handle_module_terminal_key(key)

        if key in (
            ord("q"),
            ord("Q"),
        ):
            running = self.modules.running()

            if running:
                names = ", ".join(module.spec.name for module in running)
                self.message = (
                    f"Module still running: {names}. Stop it from [U] Modules before detaching TUI."
                )
                return True

            return False

        if key == 27:
            self.screen = "dashboard"
            return True

        if self.screen == "dashboard":
            if key in (
                ord("s"),
                ord("S"),
            ):
                self.toggle_service()

            elif key in (
                ord("r"),
                ord("R"),
            ):
                ok, detail = service_action("restart")
                self.message = (f"systemd restart: {'ok' if ok else 'failed'} {detail}").strip()

            elif key in (
                ord("m"),
                ord("M"),
            ):
                result = self.ipc("reconnect_mesh")
                self.message = (
                    "Mesh reconnect requested."
                    if result.get("ok")
                    else result.get(
                        "error",
                        "failed",
                    )
                )

            elif key in (
                ord("l"),
                ord("L"),
            ):
                self.screen = "logs"

            elif key in (
                ord("n"),
                ord("N"),
            ):
                self.screen = "nodes"

            elif key in (
                ord("p"),
                ord("P"),
            ):
                self.screen = "providers"

            elif key in (
                ord("c"),
                ord("C"),
            ):
                self.edit_config()

            elif key in (
                ord("u"),
                ord("U"),
            ):
                self.screen = "modules"

        elif self.screen == "logs" and key in (
            ord("l"),
            ord("L"),
        ):
            self.screen = "dashboard"

        elif self.screen == "nodes" and key in (
            ord("n"),
            ord("N"),
        ):
            self.screen = "dashboard"

        elif self.screen == "providers":
            if key in (
                ord("p"),
                ord("P"),
            ):
                self.screen = "dashboard"

            elif key == ord("0"):
                result = self.ipc(
                    "set_provider",
                    provider="auto",
                )
                self.message = (
                    "Provider mode: auto"
                    if result.get("ok")
                    else result.get(
                        "error",
                        "failed",
                    )
                )

            elif ord("1") <= key <= ord("9"):
                response = self.ipc("providers")
                index = key - ord("1")
                providers = (
                    response.get(
                        "providers",
                        [],
                    )
                    if response.get("ok")
                    else []
                )

                if index < len(providers):
                    provider_id = providers[index]["id"]
                    result = self.ipc(
                        "set_provider",
                        provider=provider_id,
                    )
                    self.message = (
                        f"Provider: {provider_id}"
                        if result.get("ok")
                        else result.get(
                            "error",
                            "failed",
                        )
                    )

            elif key in (
                ord("t"),
                ord("T"),
            ):
                self.message = "Testing providers..."
                self.stdscr.refresh()

                result = self.ipc(
                    "test_providers",
                    timeout=10.0,
                )

                if result.get("ok"):
                    bits = [
                        f"{provider_id}={'ok' if info['ok'] else 'fail'}"
                        for provider_id, info in result["results"].items()
                    ]
                    self.message = "  ".join(bits)
                else:
                    self.message = result.get(
                        "error",
                        "test failed",
                    )

        elif self.screen == "modules":
            if key in (
                ord("u"),
                ord("U"),
            ):
                self.screen = "dashboard"

            elif ord("1") <= key <= ord("9"):
                self.open_module(key - ord("1"))

            elif key in (
                ord("k"),
                ord("K"),
            ):
                if self.last_module_id is None:
                    self.message = "No module has been opened yet."
                else:
                    module = self.modules.get(self.last_module_id)

                    if module.running:
                        module.terminate()
                        self.message = f"Stopping {module.spec.name}..."
                    else:
                        self.message = f"{module.spec.name} is not running."

        return True

    def run(self) -> None:
        curses.curs_set(0)
        self.stdscr.timeout(250)

        while True:
            self.modules.poll()
            self.stdscr.erase()

            if self.screen == "dashboard":
                self.dashboard()
            elif self.screen == "logs":
                self.logs()
            elif self.screen == "nodes":
                self.nodes()
            elif self.screen == "providers":
                self.providers()
            elif self.screen == "modules":
                self.module_menu()
            elif self.screen == "module_terminal":
                self.module_terminal()

            self.stdscr.refresh()
            key = self.stdscr.getch()

            if key != -1 and not self.handle_key(key):
                break


def run_tui(
    socket_path: str,
    config_path: Path,
) -> None:
    curses.wrapper(
        lambda stdscr: TUI(
            stdscr,
            socket_path,
            config_path,
        ).run()
    )
