# Discord observability operations

The Discord webhook exists only on the VPS. Windows sends sanitized events to the
loopback-only VPS API through the private transport; it never receives or stores the
webhook URL.

## Configuration

The dispatcher reads `YOMU_DISCORD_WEBHOOK_URL` from:

`/home/ubuntu/yomu/.secrets/discord.env`

The file must be owned by the worker account and mode `600`; the directory must be
mode `700`. Add the real value directly on the VPS, never in chat, Git, the EXE, or a
report:

```bash
install -d -m 700 /home/ubuntu/yomu/.secrets
install -m 600 /dev/null /home/ubuntu/yomu/.secrets/discord.env
${EDITOR:-vi} /home/ubuntu/yomu/.secrets/discord.env
# add only: YOMU_DISCORD_WEBHOOK_URL=<owner-managed-secret>
chmod 600 /home/ubuntu/yomu/.secrets/discord.env
```

Do not print the file. Verify only the configured flag:

```bash
curl -fsS http://127.0.0.1:18777/v1/observability/health
```

The expected safe signal is `webhook_configured:true`; the value is never returned.
The API reads the file per delivery, so a normal restart is not required for a config
change. Rotation means replacing the value in this VPS-only file and checking health.

## Diagnostics and runbook

1. Check API and worker health without dumping environments.
2. Confirm `webhook_configured:true`.
3. Send one clearly marked TEST event.
4. Inspect only sanitized delivery status, latency, event id, and dedupe result.
5. For HTTP 429/5xx or timeout, use the bounded transport test; observability must
   remain best-effort and must not fail a job.

The current dispatcher is loopback-only. `YOMU_DISCORD_ENABLED` may be used by a
future service wrapper to disable delivery; absence of a configured URL is already a
safe no-op. Chat reuse and concurrency are intentionally not part of this mission.
