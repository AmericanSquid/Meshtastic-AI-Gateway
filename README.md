# Mesh AI Gateway

Mesh AI Gateway is a Linux-oriented, headless Meshtastic gateway. It keeps the Meshtastic connection and AI routing in a daemon, while a detachable curses TUI monitors and controls the daemon through a local Unix socket.

## Features

- Meshtastic TCP/Wi-Fi, BLE, and serial transports
- Ollama and OpenAI-compatible chat providers, with priority-based failover
- Optional Hermes agent provider, run through the configured Hermes command
- Per-sender in-memory conversation history
- UTF-8-safe response truncation and chunking for mesh messages
- Mesh commands for health, provider/model selection, routing, new chats, and restart
- YAML validation and live reload
- Unix-socket IPC for status, logs, nodes, provider checks, mesh reconnects, and outbound messages
- Curses TUI for daemon/service control, logs, nodes, providers, configuration reload, and the bundled Laundry HMM module
- Foreground operation or a generated systemd user service

## Requirements and installation

Python 3.11 or newer and Meshtastic 2.7.11 or newer are required. Install the package in a virtual environment:

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e .
```

For development, install the test dependency as well:

```bash
python -m pip install -e '.[dev]'
```

The pinned `requirements.txt` is also available for reproducing the repository's environment. The Laundry HMM module additionally requires `numpy` and `hmmlearn`; these are present in that requirements file but are not package dependencies.

## Configure

Create the default configuration at `~/.config/mesh-ai-gateway/config.yaml`:

```bash
mesh-ai-gateway config init
$EDITOR ~/.config/mesh-ai-gateway/config.yaml
mesh-ai-gateway config check
```

The generated example uses a TCP Meshtastic connection. Set `mesh.transport` to `tcp`, `ble`, or `serial`, then provide the corresponding endpoint:

```yaml
mesh:
  transport: tcp
  tcp:
    host: 192.168.1.80
    port: 4403
```

AI providers are configured under `ai.providers`. Supported types are `ollama`, `openai`, and `hermes`; lower `priority` values are tried first. `ai.mode` accepts `auto` or `manual`, while runtime provider selection can be changed from the TUI or mesh commands.

An Ollama provider looks like this:

```yaml
ai:
  mode: auto
  providers:
    local:
      name: Local Ollama
      type: ollama
      priority: 1
      host: http://127.0.0.1:11434
      model: qwen3:8b
```

OpenAI-compatible providers use `base_url`, `model`, and optionally `api_key_env`. The gateway reads the key from that environment variable and does not store it in YAML:

```yaml
ai:
  providers:
    cloud:
      name: Cloud backup
      type: openai
      priority: 2
      base_url: https://example.invalid/v1
      api_key_env: CLOUD_API_KEY
      model: your-model
```

When using the generated systemd service, put the variables in `~/.config/mesh-ai-gateway/env` (mode 600), for example `CLOUD_API_KEY=...`. The service installer creates this file if it does not exist.

Other useful settings include `mesh.channel`, `mesh.reconnect`, `ai.request` retry/timeout values, `bridge` chunk/history/queue limits, `bridge.system_prompt`, `logging.level`, `logging.retain_lines`, and `ipc.socket_path`. See [`config.example.yaml`](config.example.yaml) for the complete schema and defaults. BLE requires `mesh.ble.address`; serial requires `mesh.serial.port`.

## Run

Run the daemon in the foreground and attach the TUI from another terminal:

```bash
mesh-ai-gateway daemon
mesh-ai-gateway tui
```

The TUI can start, stop, or restart the daemon through systemd, show status/logs/nodes/providers, request a mesh reconnect, edit and reload YAML, and detach with `Q`. Provider screen keys are `0` for automatic routing, `1-9` to select a provider, and `T` to run health checks.

Useful CLI commands:

```bash
mesh-ai-gateway status                 # daemon status as JSON
mesh-ai-gateway providers test         # health-check configured providers
mesh-ai-gateway config path            # print the config path
```

Messages received on the configured channel are answered on the same channel. Direct messages receive a direct reply; channel messages are broadcast. Responses are limited and split according to the `bridge` settings.

### Mesh commands

The daemon handles these commands sent over Meshtastic:

```text
!help
!health
!providers
!provider [auto|<id>]
!routing auto|manual
!models
!model <id>
!hermes
!chat
!new
!reset
!status
!restart
```

`!hermes` and `!chat` switch between the optional Hermes agent and normal provider routing. Runtime selections and conversation history are not persisted across daemon restarts.

## systemd user service

Install and enable the generated user unit:

```bash
mesh-ai-gateway service install
mesh-ai-gateway service enable
mesh-ai-gateway service start
mesh-ai-gateway service status
```

`service stop`, `service restart`, and `service disable` are also supported. To keep a user service running after logout, enable lingering separately:

```bash
loginctl enable-linger "$USER"
```

The installer also accepts `service install --system` for a system unit; use `--user NAME` when the system unit should run as a specific account.

## Project structure

```text
mesh_ai_gateway/       Package: CLI, daemon, mesh, providers, IPC, and TUI
config.example.yaml    Complete example configuration
tests/                 Configuration, provider, session, and UTF-8 utility tests
```

The daemon's conversation history is in memory, and the bundled Laundry HMM stores its recordings/model in a local SQLite file. The latter is an application-specific TUI module rather than a general gateway feature.

## Development

```bash
python -m compileall -q mesh_ai_gateway
pytest -q
```
