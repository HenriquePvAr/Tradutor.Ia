# Clean strategy

`OutputPlan` declares whether `clean` is requested; it never names a provider. The internal `yomu.clean.orchestrator` runs the local strategy first. Only `LOCAL_RECONSTRUCTION_HARD` with an authorized mask and sufficient confidence may invoke a remote adapter. SFX, invalid sources, system errors, uncertain masks, timeouts, alignment failures, seam failures, and safety violations fail closed to review.

Accepted remote output is never final by itself: it is aligned, composited over the original only inside the authorized mask, and validated with `OUTSIDE_MASK_CHANGED_PIXELS=0` and the frozen seam threshold `0.80`.
