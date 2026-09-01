from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Callable

from ..config import AIConfig, ProviderConfig
from ..errors import ProviderExhausted
from .base import AIProvider
from .providers import HermesProvider, OllamaProvider, OpenAICompatibleProvider

log = logging.getLogger(__name__)


@dataclass(slots=True)
class ProviderState:
    provider_id: str
    name: str
    type: str
    model: str | None
    status: str = "unknown"
    last_error: str | None = None


def build_provider(config: ProviderConfig) -> AIProvider:
    if config.type == "ollama":
        return OllamaProvider(config)
    if config.type == "openai":
        return OpenAICompatibleProvider(config)
    raise ValueError(f"unsupported provider type: {config.type}")


class ProviderManager:
    def __init__(
        self,
        config: AIConfig,
        provider_factory: Callable[[ProviderConfig], AIProvider] = build_provider,
    ):
        self.config = config
        self.provider_factory = provider_factory
        self.providers: dict[str, AIProvider] = {}
        self.states: dict[str, ProviderState] = {}
        self.manual_provider: str | None = None
        self.active_provider: str | None = None
        self.reconfigure(config)

    def reconfigure(self, config: AIConfig) -> None:
        previous_manual = self.manual_provider
        self.config = config
        self.providers = {
            provider_id: self.provider_factory(provider)
            for provider_id, provider in config.providers.items()
            if provider.enabled and provider.type != "hermes"
        }
        self.states = {
            provider_id: ProviderState(
                provider_id=provider_id,
                name=provider.config.name,
                type=provider.config.type,
                model=provider.config.model,
            )
            for provider_id, provider in self.providers.items()
        }
        self.manual_provider = previous_manual if previous_manual in self.providers else None
        self.active_provider = None

    def ordered_ids(self) -> list[str]:
        return sorted(
            self.providers,
            key=lambda provider_id: (
                self.providers[provider_id].config.priority,
                provider_id,
            ),
        )

    def candidates(self) -> list[str]:
        if self.manual_provider:
            return [self.manual_provider]
        return self.ordered_ids()

    def set_manual(self, provider_id: str | None) -> None:
        if provider_id in {None, "", "auto"}:
            self.manual_provider = None
            return
        if provider_id not in self.providers:
            raise ValueError(f"unknown or disabled provider: {provider_id}")
        self.manual_provider = provider_id

    async def generate(self, messages: list[dict[str, str]]) -> tuple[str, str]:
        failures: list[tuple[str, str]] = []
        for provider_id in self.candidates():
            provider = self.providers[provider_id]
            state = self.states[provider_id]
            for attempt in range(1, self.config.request.attempts_per_provider + 1):
                state.status = "working"
                state.last_error = None
                log.info(
                    "AI request provider=%s name=%r attempt=%s/%s",
                    provider_id,
                    provider.name,
                    attempt,
                    self.config.request.attempts_per_provider,
                )
                try:
                    text = await asyncio.wait_for(
                        provider.generate(messages), timeout=self.config.request.timeout
                    )
                    state.status = "ready"
                    self.active_provider = provider_id
                    return text, provider_id
                except Exception as exc:
                    error = "timeout" if isinstance(exc, asyncio.TimeoutError) else str(exc)
                    state.status = "failed"
                    state.last_error = error
                    log.warning(
                        "Provider %s (%s) attempt %s failed: %s",
                        provider_id,
                        provider.name,
                        attempt,
                        error,
                    )
                    if attempt < self.config.request.attempts_per_provider:
                        await asyncio.sleep(0.5)
            failures.append((provider.name, state.last_error or "failed"))
            if self.manual_provider:
                break
            log.info("Falling back after provider %s failed", provider.name)
        raise ProviderExhausted(failures)

    async def test_all(self) -> dict[str, dict[str, str | bool]]:
        async def test(provider_id: str, provider: AIProvider):
            try:
                ok, detail = await asyncio.wait_for(provider.healthcheck(), timeout=7.0)
            except Exception as exc:
                ok, detail = False, str(exc)
            state = self.states[provider_id]
            state.status = "ready" if ok else "failed"
            state.last_error = None if ok else detail
            return provider_id, {"ok": ok, "detail": detail}

        results = await asyncio.gather(
            *(test(pid, provider) for pid, provider in self.providers.items())
        )
        return dict(results)

    def snapshot(self) -> dict:
        return {
            "mode": "manual" if self.manual_provider else "auto",
            "selected": self.manual_provider or "auto",
            "active": self.active_provider,
            "providers": [
                {
                    "id": state.provider_id,
                    "name": state.name,
                    "type": state.type,
                    "model": state.model,
                    "status": state.status,
                    "last_error": state.last_error,
                    "priority": self.providers[state.provider_id].config.priority,
                }
                for state in sorted(
                    self.states.values(),
                    key=lambda item: self.providers[item.provider_id].config.priority,
                )
            ],
        }
