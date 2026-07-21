# CBSS Integration Boundary

Vector Lake owns Source, Evidence, Claim candidates, provenance, knowledge projections, and retrieval context.

The consuming CBSS runtime owns authority acceptance, AcceptedFact lifecycle, Aggregate state, Command, executable Policy, Decision, ActionRequest, ExecutionResult, business Event Ledger, compensation, and System-of-Record reconciliation.

`EvidencePacket` is a read-only transfer contract. It does not authorize a claim, mutate Vector Lake, or assert that the claim is an AcceptedFact. Version 1.1 includes ClaimAssessment records, source-integrity/raw-locator/lineage flags, and an export context. Evidence text requires a non-empty actor and purpose.

## Export surfaces

- MCP: `export_evidence_packet`
- CLI: `python cli.py evidence-packet <claim_id>`
- Default privacy boundary: evidence text is omitted; locators and SHA-256 hashes are returned.
- Explicit text export: add `--include-text --max-text-chars <1..10000>`.

## Contract set

- `evidence-packet.schema.json`: Vector Lake claim-candidate export.
- `source-artifact.schema.json`: source byte integrity, storage, classification, retention, and lineage.
- `extraction-run.schema.json`: deterministic extractor/parser/model provenance for one page revision.
- `claim-assessment.schema.json`: append-only review outcome over a Claim candidate; not an authority acceptance.
- `critical-decision-registry.schema.json`: CBSS-owned registry whose IDs may rank governance work.
- `claim-acceptance-record.schema.json`: CBSS authority decision over a claim candidate.
- `business-event-envelope.schema.json`: CBSS business Event Ledger envelope; it is not a Vector Lake timeline record.
- `semantic-readiness.schema.json`: read-only consumer signal kept separate from infrastructure health.

## Prohibited coupling

- Do not use the Vector Lake Markdown timeline as a CBSS business Event Ledger.
- Do not interpret a `Policy_*` page as an executable Policy version.
- Do not interpret `memory_type=decision` as a CBSS Decision Record.
- Do not write AcceptedFact state back into Vector Lake without a separately governed contract.

## Governance priority bridge

Governance items may carry `critical_decision_refs` containing explicit IDs from a CBSS `CriticalDecisionRegistry`. Only active references accepted by the caller-provided registry verifier default to `P0`; a non-empty verification string alone is insufficient, and unverified strings do not escalate priority. Contradictions, evidence gaps, and publish candidates default to `P1`; merge and missing-link work defaults to `P2`; community naming and generic suggestions default to `P3`.

Vector Lake does not infer decision relevance from titles or descriptions. An explicit `priority` value (`P0` to `P3`) overrides the default. The registry remains owned and approved by the consuming CBSS runtime; Vector Lake stores a verified snapshot used for review ordering and decision-scoped readiness. A decision is not ready unless its mapped Claim references have complete evidence, verified source integrity, raw-source locators, safe lineage, and a supported ClaimAssessment.
