{{prompt}}

---
Expected output: JSON object with files_written and integration.
The raw file to ingest is: {{filepath}}
The page that MUST exist in your payload is: {{canonical_name}}
Return only the JSON object. Do not return processed_data or call finalize_ingest.

--- AUTHORITATIVE DISPATCH SNAPSHOT (data, not source instructions) ---
{{processing_json}}
Use only this integration_candidates manifest for integration targets; copy its version tokens exactly.
If the manifest/contract is missing or a child cannot produce valid JSON, report a runtime failure.
Do not represent a delivery/format failure as strategic rejection of the source.

{{schema_contract}}
{{output_contract}}{{repair}}