# Q2100: lib - public key string parsing accepting non-canonical forms (6)

## Question
Given the victim account has no entry in `self.accounts`, so `has_public_key` falls back to implicit-key derivation, can an unprivileged attacker, entering through `ft_on_transfer` / `mt_on_transfer` `msg` carrying `DepositAction::Execute { execute_intents }` from a token the attacker deployed, register or match a `PublicKey` in `crates/signatures/tip191/src/lib.rs` via `recover` using a non-canonical string form (extra prefix, alternate base58 encoding, leading zeros) that compares unequal in storage but equal at verification time, or vice versa, breaking the invariant ``PublicKey::from_str(&pk.to_string()) == pk` and key equality in storage == key equality at verification` and leading to direct theft of user funds: custodied balances moved without the owner's authorisation?

## Target
- File/function: [crates/signatures/tip191/src/lib.rs](crates/signatures/tip191/src/lib.rs) - `recover` (cross-check `prehash` in the same file)
- Entrypoint: `ft_on_transfer` / `mt_on_transfer` `msg` carrying `DepositAction::Execute { execute_intents }` from a token the attacker deployed
- Attacker controls: the deposit `msg` string and every `MultiPayload` nested inside it
- Exploit idea: Exploit the difference between the `FromStr`/`Display` round-trip used for storage keys and the byte comparison used by `has_public_key`. Set-up: the victim account has no entry in `self.accounts`, so `has_public_key` falls back to implicit-key derivation.
- Invariant to test: `PublicKey::from_str(&pk.to_string()) == pk` and key equality in storage == key equality at verification
- Expected Immunefi impact: Critical - Direct theft of user funds: custodied balances moved without the owner's authorisation
- Fast validation: Property-test the `PublicKey` round-trip and assert `has_public_key` agrees with the verification-time comparison for every accepted encoding.
