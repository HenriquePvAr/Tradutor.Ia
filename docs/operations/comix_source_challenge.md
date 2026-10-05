# Comix: challenge-aware source flow

The Comix source path keeps the existing direct-reader and adaptive browser resolver. A browser challenge is classified as `challenge_required`; it is never bypassed or treated as a generic terminal access denial.

When a claimed URL job reaches that outcome, the worker persists `waiting_source_verification`, records the diagnostic reason, and leaves the job resumable. The UI shows the existing source-review panel with an explicit instruction for the owner to complete legitimate verification in the browser. “Continuar após verificação” resumes the same job through `/api/ui/source/verification/resume`; it does not create a second job or silently retry a scraper.

The main output-format selector was removed. The effective output contract remains owned by the existing OutputPlan; the legacy `output_format` value is retained only as compatibility metadata.

No CAPTCHA, anti-bot, fingerprint spoofing, cookies, tokens, or credentials are handled by this flow.
