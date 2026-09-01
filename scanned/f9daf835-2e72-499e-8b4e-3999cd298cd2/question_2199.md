# Q2199: near - schema/ABI form disagrees with the runtime decoder (4)

## Question
Given the JSON is hand-written rather than produced by a wallet, can an unprivileged attacker, entering through a value the attacker's contract returns into a resolve callback, exploit a difference between the declared schema for `remove` in `crates/map-utils/src/near.rs` and what the runtime `serde`/`borsh` decoder accepts, so a wallet displays one thing and the contract executes another, breaking the invariant `the set of values the runtime accepts == the set the published schema describes` and leading to direct theft of user funds: custodied balances moved without the owner's authorisation?

## Target
- File/function: [crates/map-utils/src/near.rs](crates/map-utils/src/near.rs) - `remove` (cross-check `contains_key` in the same file)
- Entrypoint: a value the attacker's contract returns into a resolve callback
- Attacker controls: the exact JSON bytes returned
- Exploit idea: Signers rely on the schema to render what they sign; any laxity in the runtime decoder is a signing-UI attack. Set-up: the JSON is hand-written rather than produced by a wallet.
- Invariant to test: the set of values the runtime accepts == the set the published schema describes
- Expected Immunefi impact: Critical - Direct theft of user funds: custodied balances moved without the owner's authorisation
- Fast validation: Generate values accepted at runtime but invalid under the schema; assert none exist.
