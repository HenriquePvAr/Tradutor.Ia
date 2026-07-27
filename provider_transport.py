"""Generic resilience contracts for outbound provider transports."""
from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
import threading
import time
from typing import Callable
from urllib.parse import urlparse


POLICY_SCHEMA_VERSION = "1"


def _hash(value: object) -> str:
    encoded = json.dumps(
        value, sort_keys=True, separators=(",", ":"), default=str
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True)
class ProviderTimeoutPolicy:
    connect_timeout_seconds: float
    read_timeout_seconds: float
    write_timeout_seconds: float
    pool_timeout_seconds: float
    total_timeout_seconds: float
    source: str
    provider: str
    operation: str
    schema_version: str = POLICY_SCHEMA_VERSION

    def __post_init__(self) -> None:
        values = (
            self.connect_timeout_seconds,
            self.read_timeout_seconds,
            self.write_timeout_seconds,
            self.pool_timeout_seconds,
            self.total_timeout_seconds,
        )
        if any(not isinstance(value, (int, float)) or value <= 0 for value in values):
            raise ValueError("provider_timeout_policy_invalid")

    @property
    def policy_hash(self) -> str:
        return _hash(asdict(self))

    def httpx_timeout(self, remaining: float | None = None):
        import httpx

        cap = self.total_timeout_seconds if remaining is None else max(0.001, remaining)
        return httpx.Timeout(
            connect=min(self.connect_timeout_seconds, cap),
            read=min(self.read_timeout_seconds, cap),
            write=min(self.write_timeout_seconds, cap),
            pool=min(self.pool_timeout_seconds, cap),
        )


@dataclass(frozen=True)
class CircuitBreakerPolicy:
    failure_threshold: int
    recovery_timeout_seconds: float
    half_open_max_calls: int
    success_threshold: int
    counted_failure_types: tuple[str, ...]
    ignored_failure_types: tuple[str, ...]
    schema_version: str = POLICY_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if (
            self.failure_threshold <= 0
            or self.recovery_timeout_seconds <= 0
            or self.half_open_max_calls <= 0
            or self.success_threshold <= 0
        ):
            raise ValueError("provider_circuit_policy_invalid")

    @property
    def policy_hash(self) -> str:
        return _hash(asdict(self))


class ProviderTransportError(RuntimeError):
    def __init__(self, reason_code: str, *, status_code: int | None = None):
        super().__init__(reason_code)
        self.reason_code = reason_code
        self.status_code = status_code


class ProviderCircuitBreaker:
    """Thread-safe in-process breaker with bounded half-open probes."""

    def __init__(
        self,
        policy: CircuitBreakerPolicy,
        *,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.policy = policy
        self.clock = clock
        self.state = "closed"
        self.consecutive_failures = 0
        self.opened_at_monotonic: float | None = None
        self.last_failure_type = ""
        self.last_success_at: float | None = None
        self._half_open_in_flight = 0
        self._half_open_successes = 0
        self._lock = threading.Lock()

    def before_call(self) -> str:
        with self._lock:
            now = self.clock()
            if self.state == "open":
                if (
                    self.opened_at_monotonic is not None
                    and now - self.opened_at_monotonic
                    >= self.policy.recovery_timeout_seconds
                ):
                    self.state = "half_open"
                    self._half_open_in_flight = 0
                    self._half_open_successes = 0
                else:
                    raise ProviderTransportError("provider_circuit_open")
            if self.state == "half_open":
                if self._half_open_in_flight >= self.policy.half_open_max_calls:
                    raise ProviderTransportError("provider_half_open_probe_rejected")
                self._half_open_in_flight += 1
            return self.state

    def record_success(self) -> None:
        with self._lock:
            self.last_success_at = self.clock()
            self.consecutive_failures = 0
            if self.state == "half_open":
                self._half_open_in_flight = max(0, self._half_open_in_flight - 1)
                self._half_open_successes += 1
                if self._half_open_successes >= self.policy.success_threshold:
                    self.state = "closed"
                    self.opened_at_monotonic = None
                    self._half_open_successes = 0

    def record_failure(self, reason_code: str) -> None:
        with self._lock:
            if self.state == "half_open":
                self._half_open_in_flight = max(0, self._half_open_in_flight - 1)
            if reason_code in self.policy.ignored_failure_types:
                return
            if reason_code not in self.policy.counted_failure_types:
                return
            self.last_failure_type = reason_code
            self.consecutive_failures += 1
            if (
                self.state == "half_open"
                or self.consecutive_failures >= self.policy.failure_threshold
            ):
                self.state = "open"
                self.opened_at_monotonic = self.clock()
                self._half_open_successes = 0

    def snapshot(self) -> dict[str, object]:
        with self._lock:
            return {
                "state": self.state,
                "consecutive_failures": self.consecutive_failures,
                "opened_at_monotonic": self.opened_at_monotonic,
                "last_failure_type": self.last_failure_type,
                "last_success_at": self.last_success_at,
                "policy_hash": self.policy.policy_hash,
            }


_BREAKERS: dict[str, ProviderCircuitBreaker] = {}
_BREAKERS_LOCK = threading.Lock()


def circuit_scope_key(provider: str, endpoint: str, operation: str) -> str:
    host = (urlparse(str(endpoint or "")).hostname or "").casefold()
    return _hash({
        "provider": str(provider or "").casefold(),
        "endpoint_host": host,
        "operation": str(operation or "").casefold(),
    })


def shared_circuit_breaker(
    scope_key: str, policy: CircuitBreakerPolicy
) -> ProviderCircuitBreaker:
    with _BREAKERS_LOCK:
        breaker = _BREAKERS.get(scope_key)
        if breaker is None or breaker.policy.policy_hash != policy.policy_hash:
            breaker = ProviderCircuitBreaker(policy)
            _BREAKERS[scope_key] = breaker
        return breaker

