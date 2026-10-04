# Gemini Safe Fallback V2

The reorganized integration lives under `yomu.clean.gemini_fallback`. It ports only the generic Authorized Mask Generator V2, the safe-clean quality contract, and the transport client. The legacy research worker and browser-auth implementation are intentionally excluded.

The generator is case-agnostic and fail-closed for ambiguous ownership. Final compositing remains restricted to the authorized mask, with the outside-mask identity gate and the seam gate preserved.
