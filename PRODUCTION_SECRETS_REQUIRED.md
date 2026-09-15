# Production secrets contract

These names are deployment-only and must never be bundled into the desktop client:

- `DEEPL_API_KEY` — server-side DeepL proxy
- `BILLING_API_KEY`, `BILLING_WEBHOOK_SECRET` — hosted billing provider
- `AD_PROVIDER_SECRET` — rewarded-ad verification
- `AYET_PUBLISHER_API_KEY` — ayeT callback HMAC verification (backend only)
- `AYET_REWARDED_ADSLOT_ID` — ayeT placement/adslot identifier (non-secret config)
- `UPDATE_SIGNING_PRIVATE_KEY` — release publisher only
- `SMTP_PASSWORD` — transactional email provider
- Supabase server credentials only where a server-side control-plane operation requires them

Use the sanitized configuration checker before activation. Values are not documented here.
