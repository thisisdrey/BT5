# Q9700: Subgroup or Curve Validation Gap in serial_fft_assign_g1

## Question
Can attacker-controlled keys or proof elements reaching `crates/aptos-crypto/src/blstrs/fft.rs::serial_fft_assign_g1` bypass subgroup, curve, or structural validity checks and later influence trusted cryptographic decisions?

## Target
- File/function: crates/aptos-crypto/src/blstrs/fft.rs::serial_fft_assign_g1
- Entrypoint: Submit a transaction, proof, signature set, or package whose cryptographic material reaches `serial_fft_assign_g1` in production validation or execution.
- Attacker controls: signature bytes, public keys, aggregated key sets, proof elements, message bytes, domain-separation inputs, and malformed encodings
- Exploit idea: Target structural validation steps for elliptic-curve points, scalars, transcripts, or proof elements.
- Invariant to test: All trusted keys and proof elements must satisfy full structural validity checks before any downstream use.
- Expected Immunefi impact: High. Acceptance of forged, stale, malformed, differently encoded, insufficiently bound, or context-confused bytecode, signatures, keyless proofs, JWK material, module metadata, write sets, or state proofs that bypass mainnet execution or verification rules.
- Fast validation: Feed invalid-curve, wrong-subgroup, or malformed-structure inputs into the parser and verifier and assert early rejection everywhere.
