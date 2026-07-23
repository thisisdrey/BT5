# Q18829: Chain Context Binding Slip in verify_signature_without_exp_check

## Question
Can attacker-controlled transaction or authenticator data reaching `types/src/jwks/rsa/mod.rs::verify_signature_without_exp_check` be validated against one chain or ledger context and then executed as if it belonged to another, enabling replay or unauthorized execution?

## Target
- File/function: types/src/jwks/rsa/mod.rs::verify_signature_without_exp_check
- Entrypoint: Submit a signed transaction, multisig payload, or keyless transaction until VM validation reaches `verify_signature_without_exp_check`.
- Attacker controls: keyless proof bytes, JWT or OIDC claim fields, ephemeral keys, nonce values, aud/iss/sub bindings, JWK material, authenticator bytes, and expiration data
- Exploit idea: Exploit any gap between the chain context used by authentication checks and the context later trusted by execution.
- Invariant to test: Authentication must bind to the exact chain and ledger context where execution happens.
- Expected Immunefi impact: High. Unauthorized transaction execution, replay, signer confusion, signature or proof misbinding, or sequence and authorization bypass that lets an unprivileged attacker cause state transitions on behalf of another user or module context.
- Fast validation: Add cross-context tests that vary chain and ledger identifiers while keeping the rest of the transaction fixed and assert strict rejection.
