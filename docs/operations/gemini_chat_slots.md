# Gemini persistent chat slots

The current VPS worker is deliberately configured as:

`1 browser → 1 slot → 1 persistent Gemini chat → max concurrency 1`

The slot state is stored as runtime metadata under `.runtime/chat-slots/slot-1.json`.
It contains slot ownership, a stable conversation id, generation sequence, last-use
time and rotation count. It never stores cookies, tokens, webhook values or browser
profile data. Chat URL is operational state only and is omitted from health/Discord.

## Ownership and queue

Every job must acquire `slot-1` with its job/request identity. A busy slot rejects a
second acquire; the existing queue keeps the second job pending until the first
releases the slot. Release returns the slot to `IDLE` without closing the chat.

The worker reports `browser_count`, `slot_count`, `active_slots`, `max_concurrency`,
`queue_depth`, `chat_generation_seq` and `chat_rotation_count` through the private
health endpoint. Concurrency remains 1; a second browser/chat is intentionally not
implemented.

## Reuse and stale-output firewall

Each request is self-contained and reuses the recorded chat. Before sending, the
provider captures a DOM baseline of message markers and generated-image sources. It
accepts only a newly observed generated-image source. If no new output is proven, it
raises a safe error and marks the slot for rotation; it never returns the last old
image as a fallback. Composer text is cleared before the frozen prompt is filled.

Safe compositing, authorized masks, seam checks and the SFX firewall remain in the
existing worker validation path.

## Rotation and recovery

Rotation is not based on image count. It is reserved for authentication loss, an
unrecoverable DOM/page, repeated generation stalls, a proven context failure or a
manual operator action. The state records a safe reason and increments
`rotation_count`; it does not silently create a new chat for each job.

After a worker restart, the state file is loaded and the registered chat/page is
validated against the attached Chrome tab. The browser profile remains the owner of
authentication. If validation is ambiguous, the worker fails closed instead of
materializing a stale output.

Discord receives only safe slot metrics: slot number, reuse status, generation
sequence and rotation count. It never receives the chat URL or conversation id.

Future concurrency uses one independent browser/page/chat per slot (`slot-1/chat-1`,
`slot-2/chat-2`). It is intentionally outside this mission.
