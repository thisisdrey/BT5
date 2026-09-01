# Q3455: lib - public key string parsing accepting non-canonical forms (14)

## Question
Given the victim account has no entry in `self.accounts`, so `has_public_key` falls back to implicit-key derivation, can an unprivileged attacker, entering through `simulate_intents(signed: Vec<MultiPayload>)` - a view call any party (including a solver's quoting path) may rely on, register or match a `PublicKey` in `crates/signatures/ton-connect/src/lib.rs` via `TonConnectPayloadSchema` using a non-canonical string form (extra prefix, alternate base58 encoding, leading zeros) that compares unequal in storage but equal at verification time, or vice versa, breaking the invariant ``PublicKey::from_str(&pk.to_string()) == pk` and key equality in storage == key equality at verification` and leading to direct theft of user funds: custodied balances moved without the owner's authorisation?

## Target
- File/function: [crates/signatures/ton-connect/src/lib.rs](crates/signatures/ton-connect/src/lib.rs) - `TonConnectPayloadSchema` (cross-check `try_prehash` in the same file)
- Entrypoint: `simulate_intents(signed: Vec<MultiPayload>)` - a view call any party (including a solver's quoting path) may rely on
- Attacker controls: the entire simulated batch and every field of each payload
- Exploit idea: Exploit the difference between the `FromStr`/`Display` round-trip used for storage keys and the byte comparison used by `has_public_key`. Set-up: the victim account has no entry in `self.accounts`, so `has_public_key` falls back to implicit-key derivation.
- Invariant to test: `PublicKey::from_str(&pk.to_string()) == pk` and key equality in storage == key equality at verification
- Expected Immunefi impact: Critical - Direct theft of user funds: custodied balances moved without the owner's authorisation
- Fast validation: Property-test the `PublicKey` round-trip and assert `has_public_key` agrees with the verification-time comparison for every accepted encoding.
