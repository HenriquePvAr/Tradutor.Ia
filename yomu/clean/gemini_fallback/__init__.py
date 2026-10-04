"""Safe Gemini fallback capability; deliberately not wired to UI/output modes."""

from .authorized_mask import generate_authorized_mask, generate_authorized_mask_v2
from .quality import evaluate_training_candidate

__all__ = ["generate_authorized_mask", "generate_authorized_mask_v2", "evaluate_training_candidate"]
