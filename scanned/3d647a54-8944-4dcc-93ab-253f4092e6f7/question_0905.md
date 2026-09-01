# Q0905: near - Cow/borrow round-trip alters the value

## Question
Given the JSON is hand-written rather than produced by a wallet, can an unprivileged attacker, entering through `execute_intents` with a hand-crafted JSON payload rather than a wallet-generated one, exploit a borrow/owned asymmetry in `entry` of `crates/map-utils/src/near.rs` so the value emitted in an event differs from the value applied to state, breaking the invariant `the value serialised into an event == the value applied to state` and leading to unauthorized state mutation of another account's authorisation configuration?

## Target
- File/function: [crates/map-utils/src/near.rs](crates/map-utils/src/near.rs) - `entry` (cross-check `contains_key` in the same file)
- Entrypoint: `execute_intents` with a hand-crafted JSON payload rather than a wallet-generated one
- Attacker controls: every byte of the JSON, including field order, duplicates, encodings and whitespace
- Exploit idea: Events borrow the intent while state applies a transformed copy; a divergence misleads every off-chain consumer. Set-up: the JSON is hand-written rather than produced by a wallet.
- Invariant to test: the value serialised into an event == the value applied to state
- Expected Immunefi impact: High - Unauthorized state mutation of another account's authorisation configuration
- Fast validation: Round-trip through `entry`; assert event and state agree.
