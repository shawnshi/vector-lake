# Controlled Pi host relay consumer — blocked source candidate

**Default OFF. Not deployed or accepted for queue consumption.** The native
migration is implemented and tested synthetically; production readiness remains
blocked by unresolved public host session-root and atomic launch-binding contracts.

## Production blockers

`pi-subagents` 0.67.0 public `resolveSubagentLaunchContract` reports:

- `code: host_required`, `severity: host-required`.
- `No sessionRoot/sessionDir was supplied; exact child session paths require the Pi host session-root policy.`

The consumer returns the fixed `relay_host_validation_pending`, leaves
`preflightVerified:false`, and refuses start. Persistence consent does **not**
waive this check. No caller-selected or fabricated session root is supplied in
production. The public delegation request cannot set `sessionRoot/sessionDir`;
the actual foreground root is selected by the native owner's private
`getSubagentSessionRoot` dependency. The parent session path is not that root.
A supported **host-resolved preflight path context** (matching actual delegation)
is needed before the gate can be completed. Do not call private helpers or add a
configuration bypass to make this candidate start.

Source evidence: package `src/api/preflight.ts:429–442`,
`src/api/delegation.ts:31–49`, and
`src/runs/foreground/subagent-executor.ts:6860–6883`. These are inspection
references, not private runtime imports.

**Atomic launch binding is an independent release blocker.** Public delegation has
no expected-digest/frozen-contract handle checked before child creation. Native
execution rediscovers configuration; a final digest check cannot undo expanded
tools, extensions, or fallback processing. `hard:0` is not proof of actual zero
tools/extensions at launch. Resolving the root alone is insufficient: a supported
host launch boundary must bind and enforce the verified contract before creating
a child. Neither gate is waived; this candidate must remain off.

## Native design and semantic changes

- Public `pi.events` structured delegation, `result:{kind:"text"}`, fresh context,
  explicit current `ctx.model.provider/ctx.model.id`, native authentication.
  `thinking:"off"` is rendered as the native `provider/id:off` launch reference;
  no model substitution or configured fallback candidate is admitted.
- Dedicated `.pi/agents/host-relay-json.md` beneath this consumer; child cwd is
  pinned to this directory. Explicit empty `tools:` and `extensions:` are
  essential. No global registration, settings changes, or parent-wide ceiling.
- Public preflight checks the **effective** selected agent/path, context/model
  candidates using a fresh metadata snapshot of **every** model returned by the
  public `ctx.modelRegistry.getAvailable()` (not just the selected model),
  explicit empty tools, skills, MCP, nested delegation, intercom,
  configured extensions, and ambient-extension exclusion. The package-owned
  `subagent-prompt-runtime.ts` is intrinsic and remains present. A
  `toolBudget:{hard:0,block:"*"}` supplies an additional native execution guard.
- The installed package's public `pi-subagents/preflight` export is resolved from
  `~/.pi/agent/npm`; Pi's extension loader handles its TypeScript imports. Version
  other than 0.67.0 fails closed. No new dependency or private runner import.
- Only matching `(requestId,ownerRunId,nodeId)` native events are accepted;
  run identity is also checked when supplied. Foreign, duplicate, and late
  events do not release an active attempt. Successful results require matching
  agent/model/launch digest, zero tool calls, text JSON, and `output.job_id`.
- Cancel/timeout sends native cancellation but **waits for a correlated terminal**.
  Missing terminal keeps busy/lock held and blocks another child. Settlement
  cleans event subscriptions/timers. Stop, session switch, model change, shutdown,
  and broker failure preserve this rule. Native cancellation settlement is not
  proof that remote HTTP naturally stopped.

Removed: `modelRegistry.complete`, custom HTTP/SSE/zstd, payload cap injection,
cap-echo/usage proof, API-specific Codex restriction, and `/relay-probe`.
The old public cap probe exhausted its retry budget and did **not** pass. There
is no automatic replacement probe and no provider traffic in preflight.

Pi owns authentication and underlying retries. There is no exactly-one-provider-
request guarantee or provider hard-token cap. `artifacts:false` does **not**
disable native `session.jsonl`: prompts/results persist in native child sessions.
The consumer never logs raw output, prompts, credentials, or provider exceptions;
its diagnostics are fixed allowlisted codes. Native persistence is a separate,
accepted data surface, not a consumer zero-retention guarantee.

## Preserved broker and bounds

`broker.py` and the producer are unchanged: singleton OS lock, one active job,
lease/consent/profile pins, persistent at-most-once claims, packet protocol/job/
attempt/generation/nonce binding, and lease revalidation before publication.
Stem begins with the attempt ID, not the independent job ID; both retain their
format checks without imposing an invalid equality.
Responses are atomic non-overwriting hard links. Failed response envelopes have
no top-level `job_id`; successful `output.job_id` remains required. Existing
schema/purpose validation stays downstream. No requeue, claim clearing, or
worker fallback is introduced.

- Input: at most 196608 UTF-8 bytes. Context admission requires a declared window
  of at least 196608 + 16384 + 1024 tokens. This explicit conservative reservation
  is **not** an exact tokenizer proof or a provider generation cap.
- Application output and IPC frames: at most 8 MiB. Existing broker publication
  remains tighter (1 MiB envelope and packet allowance). The packet envelope
  floor is 4096 bytes; broker reserves 512 bytes before IPC, so the minimum net
  `maxOutputBytes` is **3584**, enforced by both consumer IPC and result validation. Timeout is at most 900000 ms, bounded by the broker lease/deadline.
- Application result checks are not transport hard caps: native transport and
  session storage may exceed the final accepted result size.
- Existing scan bounds, symlink/reparse checks, profile-digest checks, and
  same-user ACL assumptions remain. This is not a hostile-local-user sandbox.
  Preflight is not an atomic configuration lock; drift mismatches reject results,
  but cannot retroactively undo a changed native launch.

## Commands and acceptance boundary

Factory registration creates no broker, timers, sessions, or preflight work.
Commands reject child, print, and JSON-only hosts.

- `/relay-status`: fixed codes/counts, preflight state, `nativeAcceptance:not_verified`.
- `/relay-preflight`: explicit no-network effective configuration check; currently
  ends with `relay_host_validation_pending`, not production readiness.
- `/relay-start {"python":"absolute path","runtimeProfile":"absolute path","profile":"default"}`:
  requires readiness and rechecks it; remains blocked in this candidate.
- `/relay-stop`: cancel, wait for native terminal, then release broker. If native
  settlement is absent, retains the lock and reports `relay_cancel_unsettled`.

No deployment, actual native model test, provider authentication validation, or
queue acceptance was performed. Parent owns independent review and any separately
authorized real-host validation after the public path-policy gap is resolved.

## Isolated validation and rollback

```sh
node --test extensions/host-relay-consumer/tests.mjs
python -B -m pytest -q extensions/host-relay-consumer/test_broker.py tests/test_host_relay_consumer_interop.py
```

Tests replace obsolete wire-cap assertions with synthetic event-bus state-machine,
model/tool/fallback rejection, JSON/binding/size, cancellation/timeout, lifecycle,
and fake-broker concurrency assertions. Shutdown tests inject mock terminals;
these are not actual host-shutdown evidence. Native bridge `cancelAll()/dispose()`
can suppress later terminal events during shutdown, leaving this consumer in its
no-terminal fail-closed state. No natural provider-stop or real shutdown proof is
claimed. The 4096-to-3584 test calls the unchanged broker's IPC projection with a
synthetic broker object; it starts no live consumer, reader thread, or spool. The actual public preflight is loaded
through Jiti in an isolated synthetic HOME: production missing-root rejection is
asserted first. An explicitly **test-only** root then validates discovery and
settings override rejection; it supplies no production host acceptance. No real
provider calls, live spool/DB access, or consumer process startup occurs.

Review-repair rollback: restore the four changed source files from
`native-subagent-v1/review-repair-1/before/` after verifying recorded post-repair
hashes. The agent asset is unchanged in this pass. Exact settled candidate copies
and test receipts reside in that repair directory; parent must recheck after any
later mutation hook. Prior three-file hash drift cause remains unknown: a deferred
formatter hook exists, but no report-time source copies establish its attribution.

Original migration rollback source only: verify current changed-file hashes against the task artifact,
restore `index.ts`, `model.ts`, `tests.mjs`, `README.md` from the preflight source
backups, and remove only the manifest-listed new agent file. Preserve all user
patches, broker/producer files, installed extension, queues, claims, and locks.
Backup authority: `MEMORY/scratch/controlled-relay-consumer-20260914/native-subagent-v1/preflight.json`.
No deployment rollback is needed because the installed extension was not changed.
