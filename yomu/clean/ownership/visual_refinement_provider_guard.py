"""Provider fail-closed guard for local visual refinement."""
from __future__ import annotations


FORBIDDEN_REASON = "provider_calls_forbidden_in_visual_refinement"


class ProviderCallsForbiddenInVisualRefinement(RuntimeError):
    """Raised before any visual-refinement path can reach an external provider."""


class NoProviderReviewer:
    model = "visual-refinement-no-provider"
    base_url = ""
    requests = 0
    valid_batches = 0
    repaired_batches = 0
    fallback_individual = 0
    invalid_batches = 0

    def _refuse(self, *_args, **_kwargs):
        raise ProviderCallsForbiddenInVisualRefinement(FORBIDDEN_REASON)

    review_batch = _refuse
    translate_many = _refuse
    translate = _refuse
    health_check = _refuse
