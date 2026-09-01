# Q4348: ton_connect - `try_into().ok()?` swallowing a malformed key or signature (3)

## Question
Given the victim account has no entry in `self.accounts`, so `has_public_key` falls back to implicit-key derivation, can an unprivileged attacker, entering through `ft_on_transfer` / `mt_on_transfer` `msg` carrying `DepositAction::Execute { execute_intents }` from a token the attacker deployed, reach a branch of `verify` in `contracts/defuse/core/src/payload/ton_connect.rs` where a malformed key, point-at-infinity, or non-canonical scalar is discarded with `.ok()?` in a way that changes which arm decides the result rather than rejecting outright, breaking the invariant ``verify` returns `Some(pk)` only when a cryptographically valid signature by `pk` over the exact payload exists` and leading to direct theft of user funds: custodied balances moved without the owner's authorisation?

## Target
- File/function: [contracts/defuse/core/src/payload/ton_connect.rs](contracts/defuse/core/src/payload/ton_connect.rs) - `verify` (cross-check `SignedTonConnectPayload` in the same file)
- Entrypoint: `ft_on_transfer` / `mt_on_transfer` `msg` carrying `DepositAction::Execute { execute_intents }` from a token the attacker deployed
- Attacker controls: the deposit `msg` string and every `MultiPayload` nested inside it
- Exploit idea: Supply values that fail conversion in one arm so control falls through to a weaker arm or to a default that still returns a public key. Set-up: the victim account has no entry in `self.accounts`, so `has_public_key` falls back to implicit-key derivation.
- Invariant to test: `verify` returns `Some(pk)` only when a cryptographically valid signature by `pk` over the exact payload exists
- Expected Immunefi impact: Critical - Direct theft of user funds: custodied balances moved without the owner's authorisation
- Fast validation: Fuzz `verify` with malformed keys/signatures; assert it never returns `Some` for an invalid pair.
