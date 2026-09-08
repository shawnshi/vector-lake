# Pending projection recovery completion (Stage1, not released)

Obsolete `publish_pending` is still fail-closed on ordinary reads/writes. Maintenance
backs up the full fault closure and retires the exact predecessor by CAS to
`rebuild_required`. That retirement is **not** completion and the fault bundle
remains `restorable_as_consistent_canonical_projection_snapshot=false`.

Completion records live outside the immutable v4 backup inventory:

```
META/projection-rebuild-receipts/<operation>.rebuild.json
META/projection-rebuild-receipts/<operation>.topology.json  # if topology publication is prepared
META/projection-rebuild-receipts/<operation>.completed.json
```

`operation = SHA256(canonical JSON {manifest_sha256, retirement_sha256})`.
Canonical JSON is UTF-8, sorted keys, compact separators, no NaN. Each record
has a SHA256 fingerprint over all its fields except `fingerprint`; no circular
hashes. Each file is at most 64 KiB, there are at most three fixed names per
operation and the namespace scan/admission limit is 30,000 files. Unknown names,
links/reparse entries, extra fields, malformed records and mismatches fail closed.
Exclusive creation plus file/directory sync never rewrites approved bytes. A torn
record is invalid and remains pinned; it is not automatically replaced.

The internal indexer callback (default `None`, no public CLI/MCP callback) runs
under the existing publish lock before publication. It persists the exact prepared
sidecar and original backup/retirement digests; failure prevents that publication.
The first intent binds the complete retirement successor. The optional topology
intent binds the first prepared sidecar. Both require the retirement canonical
generation. Replay uses the original immutable roots/timestamp, validates their
closure and matches the regenerated search rows before allowing the corresponding
FTS transaction; drift or missing evidence is refused, not reassigned.

Only after the actual final ready runtime, marker, locators, full object closure,
canonical generation, canonical page keys, clean topology and FTS integrity are
verified under the publish lock and SQLite transaction is `completed` recorded.
The FTS state's projection and canonical generations must both match the final
sidecar, in addition to its row-count and corpus-digest proof.
It binds the ordered intent fingerprints, exact final sidecar (generation and
both roots), and sidecar-byte SHA256. Proof failure reports
`committed-but-completion-unproven`, retaining the backup and replayable intents.

`validate_projection_rebuild_completion(manifest_path)` is the next retention
slice's read-only **historical** validator seam: it validates the strict original
bundle and record chain and returns `historically_valid=true` with
`proves_current_readiness=false`. Later legitimate canonical generations do not
invalidate a completed historical observation. A canonical-only advance followed
by maintenance starts an ordinary fresh rebuild rather than replaying completed
history. Unfinished intent replay remains exact and fail-closed.
It is not a live-readiness check,
a signature against a malicious local writer, or permission to restore a fault
bundle as a consistent snapshot.

Projection-object GC conservatively refuses while validated completion intents
remain unfinished or their bounded evidence scan is malformed/incomplete. The
evidence is bound into the preview fingerprint and rechecked under the existing
publish lock on apply, pending-GC resume and immediately before deletion. Valid
completed historical proofs do not require historical live objects or prevent
later GC. This does not unpin backups: all recovery bundles remain pinned even
with valid completion. No background completion worker, schema
migration, new public command or production recovery is included. Independent
review and later combined/live gates remain required; Stage1 is not DONE.
