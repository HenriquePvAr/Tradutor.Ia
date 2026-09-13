"""Validation-only wrapper around the canonical application spec."""
from pathlib import Path

VALIDATION_BUILD = True
base = Path(__file__).with_name("tradutor_ia.spec")
exec(compile(base.read_text(encoding="utf-8"), str(base), "exec"), globals(), globals())
