# Mesh AI Gateway

A Linux-first, headless Meshtastic AI gateway. The daemon owns the radio and AI routing; a detachable curses TUI controls and observes it over a local Unix socket.

## What v0.1 includes

- Headless daemon that stays running independently of the TUI
- Meshtastic TCP/Wi-Fi, BLE, and serial transports
- Multiple named Ollama servers
- Generic OpenAI-compatible providers for services such as Hugging Face Inference Providers and ncloud
- Hermes Agent backend via `hermes -z`
- Ordered provider failover with finite attempts and request timeouts
- Finite Meshtastic connection attempts with visible retry state
- Per-node in-memory conversation history
- UTF-8-safe response truncation and chunking
- Local mesh commands: `!status`, `!reset`, `!provider [auto|ID]`
- Unix socket IPC
- Detachable curses TUI
- Start/stop/restart daemon from the TUI through systemd
- YAML config with validation and safe reload
- journald/stdout logging plus a TUI-visible in-memory log tail

## Architecture

```text
                    Curses TUI
                        |
             +----------+----------+
             |                     |
          systemd              Unix socket
             |                     |
             +----> gateway daemon <----+
                        |                |
                Meshtastic manager   AI manager
                 TCP / BLE / Serial   Ollama x N
                        |             OpenAI-compatible x N
                    message router     Hermes
                        |
                sessions / queues
```

The TUI never owns the bridge. Pressing `Q` detaches the TUI and leaves the daemon alone.

## Install

Python 3.11+ is required.

With `uv`:

```bash
uv venv
uv pip install -e .
```

Or with pip inside a virtual environment:

```bash
python -m pip install -e .
```

The project requires Meshtastic Python 2.7.11 or newer in the 2.x line. That release line provides TCP, BLE, and serial interfaces.

## Initial configuration

```bash
mesh-ai-gateway config init
$EDITOR ~/.config/mesh-ai-gateway/config.yaml
mesh-ai-gateway config check
```

The default example is TCP/Wi-Fi because it is convenient for a stationary Linux gateway. Change `mesh.transport` to `ble` or `serial` and fill the corresponding section when needed.

### Multiple Ollama servers

Each server is just another provider instance with a stable YAML ID and a customizable display name:

```yaml
ai:
  providers:
    basement:
      name: "Big Chungus GPU Rig"
      type: ollama
      priority: 1
      host: http://192.168.1.20:11434
      model: qwen3:30b

    local:
      name: "Lil Local Guy"
      type: ollama
      priority: 2
      host: http://127.0.0.1:11434
      model: qwen3:8b
```

Renaming `name` does not change the stable provider ID used by routing.

### Hugging Face / ncloud / other OpenAI-compatible APIs

```yaml
hf:
  name: "HF Backup"
  type: openai
  priority: 3
  base_url: https://router.huggingface.co/v1
  api_key_env: HF_TOKEN
  model: your-model

ncloud:
  name: "nCloud Backup"
  type: openai
  priority: 4
  base_url: https://your-endpoint.example/v1
  api_key_env: NCLOUD_API_KEY
  model: your-model
```

API keys stay out of YAML. For the systemd service, put them in:

```text
~/.config/mesh-ai-gateway/env
```

Example:

```text
HF_TOKEN=hf_...
NCLOUD_API_KEY=...
```

The service file loads that file with `EnvironmentFile=`.

### Hermes

A Hermes provider shells out to Hermes's one-shot interface:

```yaml
hermes:
  name: "Hermes"
  type: hermes
  enabled: true
  priority: 10
  command: hermes
```

This is intentionally a simple adapter in v0.1. Hermes remains architecturally distinct enough that richer agent-specific routing can be added later.

## Run in the foreground

Useful while configuring/debugging:

```bash
mesh-ai-gateway daemon
```

Then in another terminal:

```bash
mesh-ai-gateway tui
```

Press `Q` to detach. The daemon keeps running.

## systemd user service

Install the unit using the same Python environment that contains the package:

```bash
mesh-ai-gateway service install
mesh-ai-gateway service enable
```

Useful commands:

```bash
mesh-ai-gateway service start
mesh-ai-gateway service stop
mesh-ai-gateway service restart
mesh-ai-gateway service status
```

The TUI can also start/stop/restart the service.

For a systemd **user** service to continue running when that Linux account is fully logged out, enable lingering for the account once:

```bash
loginctl enable-linger "$USER"
```

That is an OS policy choice, so the installer does not run it automatically.

## TUI controls

```text
S  Start/stop daemon
R  Restart daemon
M  Reconnect Meshtastic
P  Providers
N  Nodes
L  Logs
C  Edit YAML then reload
Q  Detach TUI
```

On the provider screen, `0` returns to automatic fallback, `1-9` pins a provider, and `T` health-checks all configured providers.

## Failure behavior

The goal is finite and visible, not elaborate:

- Mesh connection: try `mesh.reconnect.attempts` times, showing the attempt count. If all fail, remain alive and retry after `retry_after_failure` seconds.
- AI provider: try `ai.request.attempts_per_provider` times within `ai.request.timeout`, then move to the next provider.
- All AI providers fail: send a short unavailable message and keep the daemon running.
- Invalid YAML reload: reject the new config and keep the existing running configuration.
- TUI exits or crashes: daemon is unaffected.
- Daemon crashes: systemd uses `Restart=on-failure`.

## Notes / v0.1 boundaries

- Conversation history is in memory and resets when the daemon restarts.
- Runtime provider selection from the TUI or `!provider` is not persisted to YAML.
- Changing `ipc.socket_path` during a live config reload requires a daemon restart to move the socket.
- The daemon replies directly to the sending node on the incoming channel.
- BLE behavior depends on the host's Bluetooth stack and permissions.
- The Meshtastic Python callbacks are synchronous; blocking transport construction is isolated from the asyncio message workers with threads.

## Development

```bash
python -m compileall -q mesh_ai_gateway
pytest -q
```
