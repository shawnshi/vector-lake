# Operational-memory search normalization migration

The search-state `schema_version` remains 7; the database schema is unchanged.
This migration changes only the derived search
projection from SQLite/Python lowercase behavior to Python Unicode `casefold()`.
It does not apply NFKC and does not modify canonical JSON, memory IDs, memory
types, ranking weights, or source-row hashes.

## Deployment gate

Stop every old writer before deploying the new binary. Keep old binaries stopped
until the new binary has replayed the complete operational-memory corpus and the
index status is `ready` with a proof in the new normalization domain. The build
marker is a checkpoint discriminator for new code; it cannot prevent an old
binary from writing or certifying an incompatible lowercase projection.

Run the existing explicit native index-maintenance path in bounded batches. A
legacy ready proof, legacy partial checkpoint, or missing marker forces replay
from the beginning. A current build marker may resume its existing cursor. Do
not use search traffic as a migration mechanism.

For the current approximately 127,598-row corpus and the existing maximum batch
size of 10,000, plan for at least 13 maintenance batches plus final integrity
hashing. Treat this only as a work-count estimate; no elapsed-time claim is made.

## Rollback

Rollback is fenced by canonical generation, not by automatic database
replacement. After the new replay starts, do not run an old binary against that
database: old code does not understand the normalization-domain marker and can
produce a mixed index. For an interrupted migration, stop all writers and restore
a separately verified, canonical-generation-fenced offline database backup, or
finish the new replay with the new binary first. Only after the new replay is
fully `ready` may old code be restored: it must reject the new completed proof
and perform its complete lowercase rebuild before serving reads. Never resume
an old binary directly from a new partial checkpoint. Verify canonical generations
and corpus before reopening writers; do not blindly replace a database that has
received subsequent canonical writes.

Canonical operational-memory rows remain unchanged, so a derived-index rebuild
is the recovery path. `working-source-before.tar` is a source-code recovery point,
not a database backup. Create and verify a separate SQLite backup before applying
the live migration, and retain both until migration and rollback validation finish.
