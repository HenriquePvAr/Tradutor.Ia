"""Explicitly authorized NVIDIA smoke check; excluded from standard tests."""

from __future__ import annotations

import argparse

if __package__:
    from scripts.manual_network import NetworkSmokeNotAuthorized, require_network_smoke
else:  # Supports the documented ``python scripts\\...`` invocation.
    from manual_network import NetworkSmokeNotAuthorized, require_network_smoke


NVIDIA_SMOKE_OPT_IN = "ALLOW_NVIDIA_SMOKE"
SMOKE_TEXTS = (
    "Watch out!",
    "I can't believe you came back.",
    "This power... it is different.",
    "Run before the gate closes!",
)


def run_smoke(texts: tuple[str, ...] = SMOKE_TEXTS) -> int:
    """Create the real client only after both explicit opt-ins succeeded."""

    from translator_nvidia import TranslatorNvidiaBatch

    # Smoke traffic never reads or writes the production translation cache.
    translator = TranslatorNvidiaBatch(
        source_language="ingles",
        enable_cache=False,
    )
    if not translator.is_configured:
        print("NVIDIA_API_KEY nao configurada; smoke manual nao executado.")
        return 3

    translations = translator.translate_many(list(texts))
    for original, translated in zip(texts, translations):
        print(f"{original} -> {translated}")
    return 0


def main(argv: list[str] | None = None) -> int:
    try:
        require_network_smoke(NVIDIA_SMOKE_OPT_IN)
    except NetworkSmokeNotAuthorized as exc:
        print(exc)
        return 2

    parser = argparse.ArgumentParser(description="Smoke NVIDIA manual e opt-in.")
    parser.add_argument(
        "--short",
        action="store_true",
        help="Envia somente a primeira frase do smoke.",
    )
    args = parser.parse_args(argv)
    texts = SMOKE_TEXTS[:1] if args.short else SMOKE_TEXTS
    return run_smoke(texts)


if __name__ == "__main__":
    raise SystemExit(main())
