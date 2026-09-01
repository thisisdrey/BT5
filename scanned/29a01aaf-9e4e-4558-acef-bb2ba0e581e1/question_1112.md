# Q1112: seq - Cow/borrow round-trip alters the value (2)

## Question
Given the JSON is hand-written rather than produced by a wallet, can an unprivileged attacker, entering through `ft_on_transfer` / `mt_on_transfer` with a hand-crafted `msg` string, exploit a borrow/owned asymmetry in `Reversed` of `crates/serde-utils/src/seq.rs` so the value emitted in an event differs from the value applied to state, breaking the invariant `the value serialised into an event == the value applied to state` and leading to unauthorized state mutation of another account's authorisation configuration?

## Target
- File/function: [crates/serde-utils/src/seq.rs](crates/serde-utils/src/seq.rs) - `Reversed` (cross-check `serialize_as` in the same file)
- Entrypoint: `ft_on_transfer` / `mt_on_transfer` with a hand-crafted `msg` string
- Attacker controls: the whole `msg`, including whether it starts with '{' and how it is escaped
- Exploit idea: Events borrow the intent while state applies a transformed copy; a divergence misleads every off-chain consumer. Set-up: the JSON is hand-written rather than produced by a wallet.
- Invariant to test: the value serialised into an event == the value applied to state
- Expected Immunefi impact: High - Unauthorized state mutation of another account's authorisation configuration
- Fast validation: Round-trip through `Reversed`; assert event and state agree.
