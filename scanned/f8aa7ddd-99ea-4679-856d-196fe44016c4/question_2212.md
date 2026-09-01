# Q2212: lib - public key string parsing accepting non-canonical forms (7)

## Question
Given the victim account has no entry in `self.accounts`, so `has_public_key` falls back to implicit-key derivation, can an unprivileged attacker, entering through `execute_intents` relayed through a public relayer key (function-call access key restricted to `execute_intents`), register or match a `PublicKey` in `crates/signatures/erc191/src/lib.rs` via `prehash` using a non-canonical string form (extra prefix, alternate base58 encoding, leading zeros) that compares unequal in storage but equal at verification time, or vice versa, breaking the invariant ``PublicKey::from_str(&pk.to_string()) == pk` and key equality in storage == key equality at verification` and leading to direct theft of user funds: custodied balances moved without the owner's authorisation?

## Target
- File/function: [crates/signatures/erc191/src/lib.rs](crates/signatures/erc191/src/lib.rs) - `prehash` (cross-check `recover` in the same file)
- Entrypoint: `execute_intents` relayed through a public relayer key (function-call access key restricted to `execute_intents`)
- Attacker controls: the payload contents; the relayer only forwards them
- Exploit idea: Exploit the difference between the `FromStr`/`Display` round-trip used for storage keys and the byte comparison used by `has_public_key`. Set-up: the victim account has no entry in `self.accounts`, so `has_public_key` falls back to implicit-key derivation.
- Invariant to test: `PublicKey::from_str(&pk.to_string()) == pk` and key equality in storage == key equality at verification
- Expected Immunefi impact: Critical - Direct theft of user funds: custodied balances moved without the owner's authorisation
- Fast validation: Property-test the `PublicKey` round-trip and assert `has_public_key` agrees with the verification-time comparison for every accepted encoding.
