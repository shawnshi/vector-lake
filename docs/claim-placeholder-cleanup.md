# Claim placeholder cleanup

`claim_placeholder_cleanup` is a full-surface maintenance tool for deleting an
explicit list of generated placeholder claims. It is intentionally absent from
the `memory` and `readonly` MCP surfaces.

Preview with `source_claim_ids` (1–256 unique, nonempty, whitespace-free IDs),
review the bounded blockers and fingerprint, then apply the identical list with
that fingerprint. The CLI equivalent requires one or more repeated
`--claim-id` arguments and uses `--apply --confirm-fingerprint`.

Eligibility is all-or-nothing. Every claim must be Active, classify as a
page-identity-matching reshaped/entity stub, have empty source/evidence lists,
have exactly one archived fact memory, and already have its exact canonical
preimage in claim history. Memory text must still match that claim. If forensic
`infrastructure_artifact:` markers exist, they must name the same recognized
stub classification; other forensic markers block cleanup. This explicit tool
deletes only the frozen projections and relies on normal FTS-pending triggers;
it does not change the generic memory-delta forensic-retention policy.
Evidence, graph, assessment, timeline, job, other
claim/memory, and unresolved governance references fail closed. The sole queue
exception is an acknowledged `evidence-gap` whose resolution is
`research-required` for the selected claim; apply closes it as
`removed-generated-placeholder` while recording its prior resolution fields.

Preview performs no initialization, backup, or write. Apply first creates and
validates a v4 maintenance backup, verifies its copied database generations,
then replans inside the write transaction before deleting only the selected
claims and memories. Claim versions and canonical identities are preserved.
The result marks canonical projection rebuild and operational-memory FTS
maintenance as pending; operators must run the existing native maintenance
commands before final acceptance.
