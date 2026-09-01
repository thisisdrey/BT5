# Q2946: base64 - Cow/borrow round-trip alters the value (6)

## Question
Given the same key appears twice in the encoded map, can an unprivileged attacker, entering through `ft_on_transfer` / `mt_on_transfer` with a hand-crafted `msg` string, exploit a borrow/owned asymmetry in `AsBase64` of `crates/serde-utils/src/base64.rs` so the value emitted in an event differs from the value applied to state, breaking the invariant `the value serialised into an event == the value applied to state` and leading to unauthorized state mutation of another account's authorisation configuration?

## Target
- File/function: [crates/serde-utils/src/base64.rs](crates/serde-utils/src/base64.rs) - `AsBase64`
- Entrypoint: `ft_on_transfer` / `mt_on_transfer` with a hand-crafted `msg` string
- Attacker controls: the whole `msg`, including whether it starts with '{' and how it is escaped
- Exploit idea: Events borrow the intent while state applies a transformed copy; a divergence misleads every off-chain consumer. Set-up: the same key appears twice in the encoded map.
- Invariant to test: the value serialised into an event == the value applied to state
- Expected Immunefi impact: High - Unauthorized state mutation of another account's authorisation configuration
- Fast validation: Round-trip through `AsBase64`; assert event and state agree.
