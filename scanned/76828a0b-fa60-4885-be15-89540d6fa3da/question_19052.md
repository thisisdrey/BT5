# Q19052: Header Negotiation Drift in module_not_found

## Question
Can an unprivileged attacker reach `api/src/response.rs::module_not_found` with crafted content negotiation, encoding, or transport-level request details and make the API choose a parsing or response path whose security checks differ from the canonical transaction or state path?

## Target
- File/function: api/src/response.rs::module_not_found
- Entrypoint: Send a REST or BCS API request that reaches `module_not_found` through the public Aptos API surface.
- Attacker controls: request body, BCS payload bytes, path and query parameters, Accept headers, ledger versions, hashes, page sizes, and simulation flags
- Exploit idea: Target inconsistencies triggered by headers or representation negotiation rather than by changing the logical request.
- Invariant to test: Transport and representation choices must not alter the security meaning of the same logical API request.
- Expected Immunefi impact: High. Acceptance of forged, stale, malformed, differently encoded, insufficiently bound, or context-confused bytecode, signatures, keyless proofs, JWK material, module metadata, write sets, or state proofs that bypass mainnet execution or verification rules.
- Fast validation: Add tests that replay one logical request under alternate headers and encodings and assert identical parsing, authorization, and state-binding behavior.
