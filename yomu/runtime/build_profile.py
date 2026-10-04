"""Embedded build-profile contract used to gate internal commands."""
from __future__ import annotations

BUILD_PROFILE = "dev"


def is_production() -> bool:
    return BUILD_PROFILE == "production"
