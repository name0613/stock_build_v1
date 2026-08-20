# Independent reviewer instructions

The immutable bundle is evidence, not a claim. Verify the actual files, hashes, fixtures, test results, deployment evidence and screenshots. Do not trust a Developer summary. Check the S-only allowlist, dynamic universe, persistence-vs-spike logic, separate foreign ownership, robust holding level parsing, broker wording, fail-closed missing data, deterministic score version, historical safety, API filters, Docker health/persistence, NAS runtime and browser evidence.

Pay special attention to `FINMIND_CAPABILITY_EVIDENCE.json`: the token capability must have been tested live. `ACCESS_DENIED` is permission evidence, not an empty dataset. Check that the bundle has no `.env`, cookies, keys, Authorization headers, NAS password or FinMind token. The manifest hashes must match every included evidence file except the manifest itself (which cannot hash itself without recursion).

Use the fixed reviewer prompt supplied in the task. Respond exactly `OK` only when every material requirement is genuinely satisfied; otherwise return the required `CODEX_REMEDIATION_PROMPT` structure.

