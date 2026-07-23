# Q13129: Canonical Encoding Bypass in is_power_of_two

## Question
Can an unprivileged attacker reach `crates/aptos-crypto/src/blstrs/polynomials.rs::is_power_of_two` with noncanonical signatures, keys, or proof encodings that verify in one place and normalize differently in another, authorizing invalid work?

## Target
- File/function: crates/aptos-crypto/src/blstrs/polynomials.rs::is_power_of_two
- Entrypoint: Submit a transaction, proof, signature set, or package whose cryptographic material reaches `is_power_of_two` in production validation or execution.
- Attacker controls: signature bytes, public keys, aggregated key sets, proof elements, message bytes, domain-separation inputs, and malformed encodings
- Exploit idea: Target any mismatch between parse-time normalization and verification-time semantics for attacker-controlled cryptographic material.
- Invariant to test: Noncanonical encodings must be rejected before they can influence any authorization or proof-verification decision.
- Expected Immunefi impact: High. Acceptance of forged, stale, malformed, differently encoded, insufficiently bound, or context-confused bytecode, signatures, keyless proofs, JWK material, module metadata, write sets, or state proofs that bypass mainnet execution or verification rules.
- Fast validation: Add malformed and alternate-encoding vectors for the same logical key or signature and assert every noncanonical form is rejected consistently.
