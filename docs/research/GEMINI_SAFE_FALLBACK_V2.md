# Gemini Safe Fallback V2 integration

The Gemini capability remains under `yomu.clean.gemini_fallback`. This integration adds only the provider-agnostic Clean strategy boundary. It does not change Authorized Mask Generator V2, the quality metadata contract, the SFX firewall, or the training candidates `case02` and `case03`.

## Residual translation recovery and runtime wiring

The historical SAFE CLEAN capability is now connected at the CLEAN boundary through
an explicit, opt-in `YOMU_ENABLE_GEMINI_SAFE_FALLBACK` flag. Local CLEAN remains the
default and the remote result is accepted only through the existing authorized-mask
and safe-compositing strategy boundary. No mask generator, OCR engine, seam policy,
SFX firewall, or billing rule was changed.

Translation recovery is a separate text-only adapter in
`yomu/translation/gemini_fallback.py`. It runs only after a primary translation
failure or semantic-fidelity failure, never for SFX, proper names, decorative text,
blocked OCR, or failed OCR. Candidates are normalized and passed through the same
translation validator before persistence. The adapter is injected by the provider;
there is no embedded credential or historical Gemini text endpoint in this branch.
When unavailable, the path fails closed and records manual review rather than
silently accepting source-language text. Recovery is part of the original logical
translation operation and does not reserve a second YK unit.
