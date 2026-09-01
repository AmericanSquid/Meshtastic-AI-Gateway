from __future__ import annotations

import asyncio
import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .mesh.manager import MeshMessage
    from .router import MessageRouter

HELP_PAGES = (
    (
        "Mesh AI Gateway Help 1/3\n"
        "!help - Show this help\n"
        "!health - Gateway/AI health\n"
        "!providers - List providers\n"
        "!provider <id> - Select provider"
    ),
    (
        "Mesh AI Gateway Help 2/3\n"
        "!routing auto - Use routing priority\n"
        "!routing manual - Use selected provider\n"
        "!models - List models\n"
        "!model <id> - Select model"
    ),
    (
        "Mesh AI Gateway Help 3/3\n"
        "!hermes - Hermes agent mode\n"
        "!chat - Return to chat mode\n"
        "!new - Start a new chat\n"
        "!restart - Restart gateway"
    ),
)

def _current_provider_id(router: MessageRouter) -> str | None:
    providers = router.providers
    if providers.manual_provider:
        return providers.manual_provider
    if providers.active_provider:
        return providers.active_provider
    ordered = providers.ordered_ids()
    return ordered[0] if ordered else None


async def _restart_process() -> None:
    await asyncio.sleep(2.0)
    os._exit(75)


async def handle_mesh_command(
    router: MessageRouter,
    message: MeshMessage,
) -> str | None:
    raw = message.text.strip()
    if not raw.startswith("!"):
        return None

    parts = raw.split(maxsplit=1)
    command = parts[0].lower()
    argument = parts[1].strip() if len(parts) > 1 else ""

    if command == "!help":
        return HELP_PAGES

    if command == "!health":
        mesh = router.mesh.snapshot()
        ai = router.providers.snapshot()
        if router.hermes_selected:
            return f"Gateway OK | Mesh {mesh['status']} | Mode hermes | Hermes selected"
        active = ai.get("active") or "idle"
        hermes = "enabled" if router.hermes is not None else "disabled"
        return (
            f"Gateway OK | Mesh {mesh['status']} | Chat {ai['mode']} | "
            f"Active {active} | Hermes {hermes}"
        )

    if command == "!providers":
        snapshot = router.providers.snapshot()
        active = snapshot.get("active")
        entries = []
        for provider in snapshot["providers"]:
            marker = "*" if provider["id"] == active else ""
            entries.append(f"{provider['id']}{marker} ({provider['name']})")
        return "Providers: " + (", ".join(entries) if entries else "none")

    if command == "!provider":
        if not argument:
            snapshot = router.providers.snapshot()
            choices = ", ".join(item["id"] for item in snapshot["providers"])
            return f"Provider: {snapshot['selected']}. Available: {choices}"
        if argument.lower() == "hermes":
            return "Hermes is an agent. Use !hermes."
        if argument.lower() == "auto":
            router.providers.set_manual(None)
            router.hermes_selected = False
            return "Routing: auto."
        try:
            router.providers.set_manual(argument)
        except ValueError as exc:
            return str(exc)
        setattr(router, "_last_manual_provider", argument)
        router.hermes_selected = False
        return f"Provider: {argument}. Routing: manual."

    if command == "!routing":
        target = argument.lower()
        if target == "auto":
            if router.providers.manual_provider:
                setattr(router, "_last_manual_provider", router.providers.manual_provider)
            router.providers.set_manual(None)
            router.hermes_selected = False
            return "Routing: auto."
        if target == "manual":
            provider_id = (
                router.providers.manual_provider
                or getattr(router, "_last_manual_provider", None)
                or router.providers.active_provider
            )
            if not provider_id:
                return "No manual provider selected. Use !provider <id>."
            try:
                router.providers.set_manual(provider_id)
            except ValueError as exc:
                return str(exc)
            setattr(router, "_last_manual_provider", provider_id)
            router.hermes_selected = False
            return f"Routing: manual ({provider_id})."
        return "Usage: !routing auto | !routing manual"

    if command == "!new":
        router.sessions.reset(router._session_key(message))
        return "New chat."

    if command == "!models":
        provider_id = _current_provider_id(router)
        if not provider_id:
            return "No chat provider available."
        provider = router.providers.providers[provider_id]
        try:
            models = await asyncio.wait_for(provider.list_models(), timeout=10.0)
        except Exception as exc:
            return f"Could not list models for {provider_id}: {exc}"
        return f"Models ({provider_id}): " + (", ".join(models) if models else "none reported")

    if command == "!model":
        if not argument:
            return "Usage: !model <id>"
        provider_id = _current_provider_id(router)
        if not provider_id:
            return "No chat provider available."
        provider = router.providers.providers[provider_id]
        provider.config.model = argument
        if provider_id in router.providers.states:
            router.providers.states[provider_id].model = argument
        return f"Model ({provider_id}): {argument}"

    if command == "!hermes":
        if router.hermes is None:
            return "Hermes is not enabled."
        router.hermes_selected = True
        return "Hermes: on."

    if command == "!chat":
        router.hermes_selected = False
        return f"Chat: on. Routing: {router.providers.snapshot()['mode']}."

    if command == "!restart":
        asyncio.create_task(_restart_process())
        return "Gateway restarting."

    return None
