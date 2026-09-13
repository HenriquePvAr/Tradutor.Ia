"""Fail-closed server translation proxy contract with mock provider."""
from __future__ import annotations
from dataclasses import dataclass

class ProviderNotConfigured(RuntimeError): pass
class TranslationProvider:
    def translate(self, text: str, *, source: str, target: str) -> str: raise NotImplementedError
class MockTranslationProvider(TranslationProvider):
    def __init__(self): self.calls = 0
    def translate(self, text: str, *, source: str, target: str) -> str:
        self.calls += 1; return f"[{target}] {text}"
class DeepLProductionProvider(TranslationProvider):
    def __init__(self, secret: str | None): self.secret = secret
    def translate(self, text: str, *, source: str, target: str) -> str:
        if not self.secret: raise ProviderNotConfigured("provider_not_configured")
        raise RuntimeError("real_provider_disabled_in_local_phase")
@dataclass
class TranslationProxy:
    provider: TranslationProvider
    def __post_init__(self): self._results = {}
    def execute(self, *, request_id: str, text: str, source: str, target: str, authorized: bool = True) -> str:
        if not authorized: raise PermissionError("translation_not_authorized")
        if not request_id or not text or len(text) > 100_000: raise ValueError("invalid_translation_request")
        if request_id in self._results: return self._results[request_id]
        result = self.provider.translate(text, source=source, target=target); self._results[request_id] = result; return result
