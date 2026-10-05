# Gemini persistent chat slots

The validated VPS experiment is configured as:

`1 browser → 2 explicit pages → 2 persistent Gemini chats → max concurrency 2`

The experiment state is stored as runtime metadata under `.runtime/chat-slots-v2/`.
The existing slot-1 chat is migrated forward and slot-2 receives a new chat. Each
slot has an explicit browser target/page id, ownership, generation sequence, last-use
time and rotation count. Public health and Discord expose only an irreversible chat
fingerprint; never a URL or raw conversation id.

## Ownership and queue

Every job atomically acquires one healthy `IDLE` slot with its job/request identity.
With both slots busy, a third job stays queued; no slot-3 is created. Release returns
the owning slot to `IDLE` without closing its chat. A request never migrates between
slots while active.

The worker reports `browser_count`, `slot_count`, `healthy_slots`, `active_slots`,
`max_concurrency`, `queue_depth`, per-slot state, independent generation sequences and
rotation counts through the private health endpoint. The chosen architecture is one
Chrome process with two explicit pages; a Chrome crash is therefore a shared failure
domain for both slots.

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

The A+B and C+D gates proved real temporal overlap and zero cross-slot/stale output.
The small benchmark did not show a speedup (C1 136.404s; C2 154.389s), so
`max_concurrency=2` is not recommended as the production default yet. Resource peaks
were not sampled during those runs and must be measured before a default change.
