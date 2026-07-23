# Q5079: Keyless Context Confusion in dummy_proof

## Question
Can attacker-controlled keyless or JWK-related inputs reaching `types/src/keyless/groth16_sig.rs::dummy_proof` be bound to the wrong identity, audience, issuer, or nonce context and authorize a transaction for the wrong account?

## Target
- File/function: types/src/keyless/groth16_sig.rs::dummy_proof
- Entrypoint: Submit a signed transaction, multisig payload, or keyless transaction until VM validation reaches `dummy_proof`.
- Attacker controls: keyless proof bytes, JWT or OIDC claim fields, ephemeral keys, nonce values, aud/iss/sub bindings, JWK material, authenticator bytes, and expiration data
- Exploit idea: Exploit context confusion between proof fields, JWK material, and the identity that the VM eventually trusts.
- Invariant to test: Keyless proofs and JWK-backed authenticators must bind to one exact account, one issuer, one audience, and one nonce domain.
- Expected Immunefi impact: High. Acceptance of forged, stale, malformed, differently encoded, insufficiently bound, or context-confused bytecode, signatures, keyless proofs, JWK material, module metadata, write sets, or state proofs that bypass mainnet execution or verification rules.
- Fast validation: Write a focused test that swaps issuer, audience, nonce, or JWK context without changing the rest of the payload and assert strict rejection.
