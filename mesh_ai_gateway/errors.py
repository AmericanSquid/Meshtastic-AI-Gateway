class GatewayError(Exception):
    """Base application error."""


class ConfigError(GatewayError):
    """Configuration is invalid."""


class ProviderError(GatewayError):
    """An AI provider request failed."""


class ProviderExhausted(ProviderError):
    """Every eligible provider failed."""

    def __init__(self, failures: list[tuple[str, str]]):
        self.failures = failures
        detail = "; ".join(f"{name}: {error}" for name, error in failures)
        super().__init__(detail or "no providers available")


class MeshError(GatewayError):
    """Meshtastic transport error."""
