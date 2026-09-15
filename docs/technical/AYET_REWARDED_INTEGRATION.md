# ayeT Rewarded Video (local integration)

The closed-beta rewarded flow is server-authoritative and remains disabled in
production until the provider account and Supabase migration are explicitly
configured. The client only requests a short-lived session; it never credits
YK. AyeT's documented HTML5 SDK supports web/PC rewarded video, requires an
`ads.txt` entry for the publisher domain, recommends a compliant CMP, and
supports S2S callbacks. Callback verification uses the `X-Ayetstudios-
Security-Hash` SHA-256 HMAC over the alphabetically sorted, URL-encoded query
parameters. `transaction_id` is the idempotency key.

## Local contract

- Provider: `ayet`
- Reward: one permanent YK per validated callback
- Daily cap: plan-controlled (beta default 5)
- Session RPC: `create_rewarded_ad_session`
- Callback RPC: `credit_ayet_rewarded_ad`
- Secrets: `AYET_PUBLISHER_API_KEY` and adslot configuration are backend-only
- Public web property and `ads.txt` are required before sandbox/live ads
- Feature flag `rewarded_ads_enabled` remains off; no remote migration/deploy

The migration `20260915153000_ayet_rewarded_sessions.sql` is source-only in
this branch. It must be reviewed and applied remotely before enabling the
provider. The callback always returns HTTP 200 for malformed/invalid events to
avoid provider retry storms, while crediting only after HMAC, session, expiry,
plan/flag, and unique transaction checks succeed.

Passive ads are intentionally not implemented: ayeT rewarded video is a
voluntary video placement, not a verifiable passive impression. A separate
passive provider decision is required.

