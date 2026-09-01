from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from typing import Any


# Keep stdout exclusively for the JSON protocol. Hermes may print during import
# or tool execution, so send all ordinary output to stderr instead.
_protocol_stdout = sys.stdout
sys.stdout = sys.stderr


def _send(payload: dict[str, Any]) -> None:
    _protocol_stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
    _protocol_stdout.flush()


def _clarify_callback(question: str, choices=None, multi_select=False) -> str:
    if choices:
        if multi_select:
            return f"[No interactive user is available. Pick the best subset from {choices} and continue.]"
        return f"[No interactive user is available. Pick the best option from {choices} and continue.]"
    return "[No interactive user is available. Make the most reasonable assumption and continue.]"


def _effective_model(cfg: dict, override: str | None) -> tuple[str, str | None]:
    from hermes_cli.models import detect_provider_for_model

    model_cfg = cfg.get("model") or {}
    if isinstance(model_cfg, str):
        configured_model = model_cfg
        configured_provider = None
    else:
        raw_model = model_cfg.get("default") or model_cfg.get("model") or ""
        if isinstance(raw_model, dict):
            from hermes_cli.config import split_model_config_default

            configured_model, _ = split_model_config_default(raw_model)
        else:
            configured_model = str(raw_model or "")
        configured_provider = str(model_cfg.get("provider") or "").strip() or None

    env_model = os.getenv("HERMES_INFERENCE_MODEL", "").strip()
    model = (override or "").strip() or env_model or configured_model
    provider = configured_provider

    if override or env_model:
        detected = detect_provider_for_model(
            model,
            provider or os.getenv("HERMES_INFERENCE_PROVIDER", "").strip() or "auto",
        )
        if detected:
            provider, model = detected

    return model, provider


def _build_agent(model_override: str | None):
    from hermes_cli.config import load_config
    from hermes_cli.fallback_config import get_fallback_chain
    from hermes_cli.mcp_startup import ensure_mcp_discovery_before_agent_build
    from hermes_cli.runtime_provider import resolve_runtime_provider
    from hermes_cli.tools_config import _get_platform_tools
    from hermes_state import SessionDB
    from run_agent import AIAgent

    cfg = load_config()

    terminal_cfg = cfg.get("terminal") or {}
    terminal_cwd = terminal_cfg.get("cwd") if isinstance(terminal_cfg, dict) else None
    if isinstance(terminal_cwd, str) and terminal_cwd.strip():
        try:
            os.chdir(os.path.expanduser(terminal_cwd.strip()))
        except OSError:
            logging.getLogger(__name__).warning("Could not use Hermes terminal.cwd: %s", terminal_cwd)

    model, provider = _effective_model(cfg, model_override)
    runtime = resolve_runtime_provider(
        requested=provider,
        target_model=model or None,
    )

    ensure_mcp_discovery_before_agent_build(
        logger=logging.getLogger(__name__),
        single_query=True,
    )

    toolsets = sorted(_get_platform_tools(cfg, "cli"))
    session_db = SessionDB()
    fallback = get_fallback_chain(cfg)

    agent = AIAgent(
        api_key=runtime.get("api_key"),
        base_url=runtime.get("base_url"),
        provider=runtime.get("provider"),
        requested_provider=runtime.get("requested_provider"),
        api_mode=runtime.get("api_mode"),
        model=model,
        enabled_toolsets=toolsets,
        quiet_mode=True,
        platform="cli",
        session_db=session_db,
        credential_pool=runtime.get("credential_pool"),
        fallback_model=fallback or None,
        clarify_callback=_clarify_callback,
    )
    agent.suppress_status_output = True
    agent.stream_delta_callback = None
    agent.tool_gen_callback = None

    return agent, session_db, runtime


def _close_agent(agent, session_db) -> None:
    if agent is not None:
        try:
            messages = getattr(agent, "_session_messages", None)
            if isinstance(messages, list):
                agent.shutdown_memory_provider(messages)
            else:
                agent.shutdown_memory_provider()
        except Exception:
            logging.getLogger(__name__).debug("Hermes memory cleanup failed", exc_info=True)
        try:
            agent.close()
        except Exception:
            logging.getLogger(__name__).debug("Hermes agent cleanup failed", exc_info=True)
    if session_db is not None:
        try:
            session_db.close()
        except Exception:
            logging.getLogger(__name__).debug("Hermes session DB cleanup failed", exc_info=True)


def main() -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--model")
    args = parser.parse_args()

    # Match the non-interactive behavior the gateway previously got from -z.
    os.environ.setdefault("HERMES_YOLO_MODE", "1")
    os.environ.setdefault("HERMES_ACCEPT_HOOKS", "1")

    agent = None
    session_db = None
    history: list[dict[str, Any]] = []

    try:
        agent, session_db, runtime = _build_agent(args.model)
        _send(
            {
                "ok": True,
                "status": "ready",
                "model": getattr(agent, "model", args.model or ""),
                "provider": runtime.get("provider") or getattr(agent, "provider", ""),
            }
        )

        for raw_line in sys.stdin:
            raw_line = raw_line.strip()
            if not raw_line:
                continue

            try:
                request = json.loads(raw_line)
                operation = request.get("op")

                if operation == "generate":
                    prompt = str(request.get("prompt") or "").strip()
                    if not prompt:
                        _send({"ok": False, "error": "empty prompt"})
                        continue

                    system_prompt = str(request.get("system_prompt") or "").strip()
                    result = agent.run_conversation(
                        user_message=prompt,
                        system_message=system_prompt or None,
                        conversation_history=history,
                    )
                    messages = result.get("messages")
                    if isinstance(messages, list):
                        history = messages
                    _send(
                        {
                            "ok": True,
                            "response": result.get("final_response") or "",
                        }
                    )
                    continue

                if operation == "reset":
                    history = []
                    agent.reset_session_state()
                    _send({"ok": True, "status": "reset"})
                    continue

                if operation == "stop":
                    _send({"ok": True, "status": "stopping"})
                    break

                _send({"ok": False, "error": f"unknown operation: {operation}"})
            except Exception as exc:
                logging.getLogger(__name__).exception("Hermes worker request failed")
                _send({"ok": False, "error": f"{type(exc).__name__}: {exc}"})

        return 0
    except Exception as exc:
        logging.getLogger(__name__).exception("Hermes worker startup failed")
        _send({"ok": False, "error": f"{type(exc).__name__}: {exc}"})
        return 1
    finally:
        _close_agent(agent, session_db)


if __name__ == "__main__":
    raise SystemExit(main())
