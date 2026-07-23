# Q6550: Secondary-Signer Escalation in legacy_script_signature_checks

## Question
Can `third_party/move/move-bytecode-verifier/src/script_signature.rs::legacy_script_signature_checks` be reached with crafted secondary-signer, multisig, or fee-payer metadata that is validated under one ordering or scope but executed under another, granting unauthorized authority?

## Target
- File/function: third_party/move/move-bytecode-verifier/src/script_signature.rs::legacy_script_signature_checks
- Entrypoint: Submit a signed transaction, multisig payload, or keyless transaction until VM validation reaches `legacy_script_signature_checks`.
- Attacker controls: signed transaction bytes, authenticator variants, secondary signer data, fee-payer fields, sequence numbers, expirations, chain IDs, and payload contents
- Exploit idea: Target normalization bugs in secondary signer lists, multisig metadata, or fee-payer handling that let authority expand during execution.
- Invariant to test: Signer ordering, threshold rules, and fee-payer scope must be canonical and preserved exactly from validation to execution.
- Expected Immunefi impact: High. Unauthorized transaction execution, replay, signer confusion, signature or proof misbinding, or sequence and authorization bypass that lets an unprivileged attacker cause state transitions on behalf of another user or module context.
- Fast validation: Add a regression test that permutes secondary signers, thresholds, and fee-payer metadata and asserts one canonical authorization outcome.
