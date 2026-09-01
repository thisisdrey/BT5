# Q1891: hex - schema/ABI form disagrees with the runtime decoder (2)

## Question
Given the JSON is hand-written rather than produced by a wallet, can an unprivileged attacker, entering through `ft_on_transfer` / `mt_on_transfer` with a hand-crafted `msg` string, exploit a difference between the declared schema for `AsHex` in `crates/serde-utils/src/hex.rs` and what the runtime `serde`/`borsh` decoder accepts, so a wallet displays one thing and the contract executes another, breaking the invariant `the set of values the runtime accepts == the set the published schema describes` and leading to direct theft of user funds: custodied balances moved without the owner's authorisation?

## Target
- File/function: [crates/serde-utils/src/hex.rs](crates/serde-utils/src/hex.rs) - `AsHex`
- Entrypoint: `ft_on_transfer` / `mt_on_transfer` with a hand-crafted `msg` string
- Attacker controls: the whole `msg`, including whether it starts with '{' and how it is escaped
- Exploit idea: Signers rely on the schema to render what they sign; any laxity in the runtime decoder is a signing-UI attack. Set-up: the JSON is hand-written rather than produced by a wallet.
- Invariant to test: the set of values the runtime accepts == the set the published schema describes
- Expected Immunefi impact: Critical - Direct theft of user funds: custodied balances moved without the owner's authorisation
- Fast validation: Generate values accepted at runtime but invalid under the schema; assert none exist.
