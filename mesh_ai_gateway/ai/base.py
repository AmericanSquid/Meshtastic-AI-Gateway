from __future__ import annotations

from abc import ABC, abstractmethod

from ..config import ProviderConfig


class AIProvider(ABC):
    def __init__(self, config: ProviderConfig):
        self.config = config

    @property
    def provider_id(self) -> str:
        return self.config.provider_id

    @property
    def name(self) -> str:
        return self.config.name

    @abstractmethod
    async def generate(self, messages: list[dict[str, str]]) -> str:
        raise NotImplementedError

    @abstractmethod
    async def healthcheck(self) -> tuple[bool, str]:
        raise NotImplementedError

    async def list_models(self) -> list[str]:
        return []
