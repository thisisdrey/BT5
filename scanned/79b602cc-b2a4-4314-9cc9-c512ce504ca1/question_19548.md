# Q19548: Dependency Substitution Slip in get_allowed_structs

## Question
Can `aptos-move/aptos-vm/src/verifier/transaction_arg_validation.rs::get_allowed_structs` accept code under one dependency graph while execution later resolves attacker-controlled dependencies differently, enabling bypass of bytecode or layout assumptions?

## Target
- File/function: aptos-move/aptos-vm/src/verifier/transaction_arg_validation.rs::get_allowed_structs
- Entrypoint: Publish a package or submit a script or entry-function payload whose bytecode or metadata is processed by `get_allowed_structs`.
- Attacker controls: module and package bytes, dependency graphs, identifiers, type tags, constants, metadata sections, script payloads, and serialized layouts
- Exploit idea: Target inconsistencies between verified dependencies and the ones later loaded or linked at runtime.
- Invariant to test: Verified code must execute only with the exact dependency graph and linking context that verification assumed.
- Expected Immunefi impact: High. Acceptance of forged, stale, malformed, differently encoded, insufficiently bound, or context-confused bytecode, signatures, keyless proofs, JWK material, module metadata, write sets, or state proofs that bypass mainnet execution or verification rules.
- Fast validation: Build packages whose safety depends on dependency identity and assert all substitution attempts are rejected before execution.
