# Q5607: signature - deterministic nonce or key derivation collision (7)

## Question
Given the victim has registered exactly one public key, of a different curve than the one submitted, can an unprivileged attacker, entering through `ft_on_transfer` / `mt_on_transfer` `msg` carrying `DepositAction::Execute { execute_intents }` from a token the attacker deployed, cause two distinct inputs to `example_secp256k1` in `contracts/defuse/core/src/signature.rs` to derive the same key, tweak or account identity, so one party's authorisation applies to another's assets, breaking the invariant `distinct derivation inputs to `example_secp256k1` == distinct derived outputs` and leading to direct theft of user funds: custodied balances moved without the owner's authorisation?

## Target
- File/function: [contracts/defuse/core/src/signature.rs](contracts/defuse/core/src/signature.rs) - `example_secp256k1` (cross-check `example_ed25519` in the same file)
- Entrypoint: `ft_on_transfer` / `mt_on_transfer` `msg` carrying `DepositAction::Execute { execute_intents }` from a token the attacker deployed
- Attacker controls: the deposit `msg` string and every `MultiPayload` nested inside it
- Exploit idea: Attack the concatenation used before hashing: unescaped separators, attacker-chosen sub-identifiers, or a length field that is not committed. Set-up: the victim has registered exactly one public key, of a different curve than the one submitted.
- Invariant to test: distinct derivation inputs to `example_secp256k1` == distinct derived outputs
- Expected Immunefi impact: Critical - Direct theft of user funds: custodied balances moved without the owner's authorisation
- Fast validation: Property-test `example_secp256k1` for collisions across adversarially chosen inputs containing the separator byte.
