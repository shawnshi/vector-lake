# Source identity and physical paths

Extraction keeps two source references. `identity_ref` preserves the historical
normalization used by Source and Evidence IDs. `physical_ref` retains the declared
reference used to resolve bytes, locators, independence, and displayed raw-source
fields.

Equivalent normalized physical aliases deduplicate. If one legacy identity maps to
different physical references, extraction fails before reading files. Artifact and
locator configuration prefers the physical key, falls back to the legacy key, and
fails closed when both keys contain different values.

Before artifact resolution, extraction validates every physical/legacy artifact
and locator configuration pair. Governance then treats `artifact_id` ownership as
immutable: a batch may repeat compatible updates from the same `source_id`, but a
different incoming or existing owner is rejected before foundation writes. Stored
column/JSON identity disagreement also fails closed; byte equality never selects,
merges, or rekeys an owner.
