from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .errors import ConfigError
from .paths import default_config_path, default_socket_path


@dataclass(slots=True)
class RetryConfig:
    attempts: int = 3
    timeout: float = 10.0
    delay: float = 3.0
    retry_after_failure: float = 30.0


@dataclass(slots=True)
class TCPConfig:
    host: str = "127.0.0.1"
    port: int = 4403


@dataclass(slots=True)
class BLEConfig:
    address: str | None = None


@dataclass(slots=True)
class SerialConfig:
    port: str | None = None


@dataclass(slots=True)
class MeshConfig:
    transport: str = "tcp"
    tcp: TCPConfig = field(default_factory=TCPConfig)
    ble: BLEConfig = field(default_factory=BLEConfig)
    serial: SerialConfig = field(default_factory=SerialConfig)
    channel: int = 0
    reconnect: RetryConfig = field(default_factory=RetryConfig)


@dataclass(slots=True)
class AIRequestConfig:
    attempts_per_provider: int = 2
    timeout: float = 20.0


@dataclass(slots=True)
class ProviderConfig:
    provider_id: str
    name: str
    type: str
    enabled: bool = True
    priority: int = 100
    model: str | None = None
    host: str | None = None
    base_url: str | None = None
    api_key_env: str | None = None
    options: dict[str, Any] = field(default_factory=dict)
    keep_alive: str | int | None = None
    command: str = "hermes"


@dataclass(slots=True)
class AIConfig:
    mode: str = "auto"
    request: AIRequestConfig = field(default_factory=AIRequestConfig)
    providers: dict[str, ProviderConfig] = field(default_factory=dict)


@dataclass(slots=True)
class BridgeConfig:
    chunk_bytes: int = 180
    chunk_delay: float = 1.0
    response_max_bytes: int = 600
    history_messages: int = 12
    queue_size: int = 50
    max_concurrent_requests: int = 2
    system_prompt: str = (
        "You are communicating over Meshtastic. Keep responses concise and "
        "appropriate for a low-bandwidth radio connection."
    )


@dataclass(slots=True)
class LoggingConfig:
    level: str = "INFO"
    retain_lines: int = 1000


@dataclass(slots=True)
class IPCConfig:
    socket_path: str = field(default_factory=lambda: str(default_socket_path()))


@dataclass(slots=True)
class AppConfig:
    mesh: MeshConfig = field(default_factory=MeshConfig)
    ai: AIConfig = field(default_factory=AIConfig)
    bridge: BridgeConfig = field(default_factory=BridgeConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)
    ipc: IPCConfig = field(default_factory=IPCConfig)
    source_path: Path | None = None


def _mapping(value: Any, path: str) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ConfigError(f"{path} must be a mapping")
    return value


def _positive_int(value: Any, path: str, default: int) -> int:
    value = default if value is None else value
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ConfigError(f"{path} must be a positive integer")
    return value


def _nonnegative_int(value: Any, path: str, default: int) -> int:
    value = default if value is None else value
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ConfigError(f"{path} must be a non-negative integer")
    return value


def _positive_float(value: Any, path: str, default: float) -> float:
    value = default if value is None else value
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        raise ConfigError(f"{path} must be a positive number")
    return float(value)


def _nonnegative_float(value: Any, path: str, default: float) -> float:
    value = default if value is None else value
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        raise ConfigError(f"{path} must be a non-negative number")
    return float(value)


def _boolean(value: Any, path: str, default: bool) -> bool:
    value = default if value is None else value
    if not isinstance(value, bool):
        raise ConfigError(f"{path} must be true or false")
    return value


def _parse_retry(raw: dict[str, Any]) -> RetryConfig:
    return RetryConfig(
        attempts=_positive_int(raw.get("attempts"), "mesh.reconnect.attempts", 3),
        timeout=_positive_float(raw.get("timeout"), "mesh.reconnect.timeout", 10.0),
        delay=_nonnegative_float(raw.get("delay"), "mesh.reconnect.delay", 3.0),
        retry_after_failure=_positive_float(
            raw.get("retry_after_failure"), "mesh.reconnect.retry_after_failure", 30.0
        ),
    )


def load_config(path: str | Path | None = None) -> AppConfig:
    source = Path(path).expanduser() if path else default_config_path()
    if not source.exists():
        raise ConfigError(f"config not found: {source}. Run 'mesh-ai-gateway config init' first.")
    try:
        raw = yaml.safe_load(source.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"invalid YAML in {source}: {exc}") from exc
    except OSError as exc:
        raise ConfigError(f"cannot read {source}: {exc}") from exc
    root = _mapping(raw, "root")

    mesh_raw = _mapping(root.get("mesh"), "mesh")
    transport = str(mesh_raw.get("transport", "tcp")).lower()
    if transport not in {"tcp", "ble", "serial"}:
        raise ConfigError("mesh.transport must be one of: tcp, ble, serial")
    tcp_raw = _mapping(mesh_raw.get("tcp"), "mesh.tcp")
    ble_raw = _mapping(mesh_raw.get("ble"), "mesh.ble")
    serial_raw = _mapping(mesh_raw.get("serial"), "mesh.serial")
    reconnect_raw = _mapping(mesh_raw.get("reconnect"), "mesh.reconnect")
    tcp = TCPConfig(
        host=str(tcp_raw.get("host", "127.0.0.1")),
        port=_positive_int(tcp_raw.get("port"), "mesh.tcp.port", 4403),
    )
    ble = BLEConfig(address=ble_raw.get("address"))
    serial = SerialConfig(port=serial_raw.get("port"))
    if transport == "tcp" and not tcp.host.strip():
        raise ConfigError("mesh.tcp.host cannot be empty")
    if transport == "ble" and not ble.address:
        raise ConfigError("mesh.ble.address is required when mesh.transport=ble")
    if transport == "serial" and not serial.port:
        raise ConfigError("mesh.serial.port is required when mesh.transport=serial")
    mesh = MeshConfig(
        transport=transport,
        tcp=tcp,
        ble=ble,
        serial=serial,
        channel=_nonnegative_int(mesh_raw.get("channel"), "mesh.channel", 0),
        reconnect=_parse_retry(reconnect_raw),
    )

    ai_raw = _mapping(root.get("ai"), "ai")
    mode = str(ai_raw.get("mode", "auto")).lower()
    if mode not in {"auto", "manual"}:
        raise ConfigError("ai.mode must be 'auto' or 'manual'")
    req_raw = _mapping(ai_raw.get("request"), "ai.request")
    request = AIRequestConfig(
        attempts_per_provider=_positive_int(
            req_raw.get("attempts_per_provider"),
            "ai.request.attempts_per_provider",
            2,
        ),
        timeout=_positive_float(req_raw.get("timeout"), "ai.request.timeout", 20.0),
    )
    providers_raw = _mapping(ai_raw.get("providers"), "ai.providers")
    providers: dict[str, ProviderConfig] = {}
    for provider_id, value in providers_raw.items():
        if not isinstance(provider_id, str) or not provider_id.strip():
            raise ConfigError("ai.providers keys must be non-empty strings")
        item = _mapping(value, f"ai.providers.{provider_id}")
        ptype = str(item.get("type", "")).lower()
        if ptype not in {"ollama", "openai", "hermes"}:
            raise ConfigError(f"ai.providers.{provider_id}.type must be ollama, openai, or hermes")
        options = item.get("options") or {}
        if not isinstance(options, dict):
            raise ConfigError(f"ai.providers.{provider_id}.options must be a mapping")
        provider = ProviderConfig(
            provider_id=provider_id,
            name=str(item.get("name", provider_id)),
            type=ptype,
            enabled=_boolean(item.get("enabled"), f"ai.providers.{provider_id}.enabled", True),
            priority=_positive_int(
                item.get("priority"), f"ai.providers.{provider_id}.priority", 100
            ),
            model=item.get("model"),
            host=item.get("host"),
            base_url=item.get("base_url"),
            api_key_env=item.get("api_key_env"),
            options=options,
            keep_alive=item.get("keep_alive"),
            command=str(item.get("command", "hermes")),
        )
        if ptype == "ollama":
            if not provider.host:
                raise ConfigError(f"ai.providers.{provider_id}.host is required")
            if not provider.model:
                raise ConfigError(f"ai.providers.{provider_id}.model is required")
        elif ptype == "openai":
            if not provider.base_url:
                raise ConfigError(f"ai.providers.{provider_id}.base_url is required")
            if not provider.model:
                raise ConfigError(f"ai.providers.{provider_id}.model is required")
        providers[provider_id] = provider
    if not any(p.enabled for p in providers.values()):
        raise ConfigError("at least one AI provider must be enabled")

    bridge_raw = _mapping(root.get("bridge"), "bridge")
    bridge = BridgeConfig(
        chunk_bytes=_positive_int(bridge_raw.get("chunk_bytes"), "bridge.chunk_bytes", 180),
        chunk_delay=_nonnegative_float(bridge_raw.get("chunk_delay"), "bridge.chunk_delay", 1.0),
        response_max_bytes=_positive_int(
            bridge_raw.get("response_max_bytes"), "bridge.response_max_bytes", 600
        ),
        history_messages=_positive_int(
            bridge_raw.get("history_messages"), "bridge.history_messages", 12
        ),
        queue_size=_positive_int(bridge_raw.get("queue_size"), "bridge.queue_size", 50),
        max_concurrent_requests=_positive_int(
            bridge_raw.get("max_concurrent_requests"),
            "bridge.max_concurrent_requests",
            2,
        ),
        system_prompt=str(bridge_raw.get("system_prompt", BridgeConfig().system_prompt)),
    )
    if bridge.chunk_bytes > bridge.response_max_bytes:
        raise ConfigError("bridge.chunk_bytes cannot exceed bridge.response_max_bytes")

    log_raw = _mapping(root.get("logging"), "logging")
    logging_cfg = LoggingConfig(
        level=str(log_raw.get("level", "INFO")).upper(),
        retain_lines=_positive_int(log_raw.get("retain_lines"), "logging.retain_lines", 1000),
    )

    ipc_raw = _mapping(root.get("ipc"), "ipc")
    socket_path = str(ipc_raw.get("socket_path") or default_socket_path())
    ipc = IPCConfig(socket_path=socket_path)

    return AppConfig(
        mesh=mesh,
        ai=AIConfig(mode=mode, request=request, providers=providers),
        bridge=bridge,
        logging=logging_cfg,
        ipc=ipc,
        source_path=source,
    )


def example_config() -> str:
    return """\
# Mesh AI Gateway configuration
mesh:
  transport: tcp

  tcp:
    host: 192.168.1.80
    port: 4403

  ble:
    address: null

  serial:
    port: null

  channel: 0

  reconnect:
    attempts: 3
    timeout: 10
    delay: 3
    retry_after_failure: 30

ai:
  mode: auto

  request:
    attempts_per_provider: 2
    timeout: 20

  providers:
    local:
      name: "Lil Local Guy"
      type: ollama
      enabled: true
      priority: 1
      host: http://127.0.0.1:11434
      model: qwen3:8b
      keep_alive: 30m
      options:
        temperature: 0.7
        num_ctx: 8192
        num_predict: 180

    # hf:
    #   name: "HF Backup"
    #   type: openai
    #   enabled: true
    #   priority: 2
    #   base_url: https://router.huggingface.co/v1
    #   api_key_env: HF_TOKEN
    #   model: your-model

    # ncloud:
    #   name: "nCloud Backup"
    #   type: openai
    #   enabled: true
    #   priority: 3
    #   base_url: https://your-ncloud-endpoint.example/v1
    #   api_key_env: NCLOUD_API_KEY
    #   model: your-model

    # hermes:
    #   name: "Hermes"
    #   type: hermes
    #   enabled: false
    #   priority: 10
    #   command: hermes

bridge:
  chunk_bytes: 180
  chunk_delay: 1.0
  response_max_bytes: 600
  history_messages: 12
  queue_size: 50
  max_concurrent_requests: 2

  system_prompt: |
    You are communicating over Meshtastic.
    Keep responses concise and appropriate for a low-bandwidth radio connection.

logging:
  level: INFO
  retain_lines: 1000

# ipc:
#   socket_path: /run/user/1000/mesh-ai-gateway/control.sock
"""
