# Q3552: near - schema/ABI form disagrees with the runtime decoder (7)

## Question
Given the same key appears twice in the encoded map, can an unprivileged attacker, entering through `mt_batch_transfer_call` / `mt_withdraw` with attacker-chosen string arguments, exploit a difference between the declared schema for `remove` in `crates/map-utils/src/near.rs` and what the runtime `serde`/`borsh` decoder accepts, so a wallet displays one thing and the contract executes another, breaking the invariant `the set of values the runtime accepts == the set the published schema describes` and leading to direct theft of user funds: custodied balances moved without the owner's authorisation?

## Target
- File/function: [crates/map-utils/src/near.rs](crates/map-utils/src/near.rs) - `remove` (cross-check `insert` in the same file)
- Entrypoint: `mt_batch_transfer_call` / `mt_withdraw` with attacker-chosen string arguments
- Attacker controls: `token_ids`, `memo`, `msg` and every numeric field supplied as a string
- Exploit idea: Signers rely on the schema to render what they sign; any laxity in the runtime decoder is a signing-UI attack. Set-up: the same key appears twice in the encoded map.
- Invariant to test: the set of values the runtime accepts == the set the published schema describes
- Expected Immunefi impact: Critical - Direct theft of user funds: custodied balances moved without the owner's authorisation
- Fast validation: Generate values accepted at runtime but invalid under the schema; assert none exist.
