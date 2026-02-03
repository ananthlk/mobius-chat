# Config (editable, no code changes)

## payer_normalization.yaml

Maps payer names (user input, state extractor, or subsidiary/parent names) to a **canonical** token used for RAG filtering (`document_payer`). Update this file when:

- Adding a new payer you have data for
- Adding parent/subsidiary or alternate names for an existing payer
- Aligning with tokens in your published RAG index (Vertex `document_payer` namespace)

**Override path:** set `PAYER_NORMALIZATION_CONFIG` to the full path of a YAML file. Default: `config/payer_normalization.yaml` under the mobius-chat root.

**Format:** `payers` is a list of `{ canonical: "Name", aliases: ["Alias1", ...] }`. Each alias (case-insensitive) maps to that canonical; the canonical value is what is sent to RAG.

**If payer filter doesn’t work:** RAG filters by exact `document_payer` in the index. If documents were published with different payer strings (e.g. `"Sunshine"` or `"UnitedHealthcare"`), the filter won’t match. Run `python scripts/check_rag_payer_names.py` to list distinct `document_payer` values in `published_rag_metadata` and compare to canonicals. Fix by either (1) normalizing document metadata to canonicals and re-publishing, or (2) adding the index values as aliases and ensuring the canonical matches what’s in the index (or updating the index to use canonicals).
